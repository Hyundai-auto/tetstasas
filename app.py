import os
import asyncio
import json
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

EXTERNAL_CHECKOUT_URL = "https://pay.meuservicomei.com.br/r/a51L1PhTl58c6S86"
EXTERNAL_BASE_URL = "https://pay.meuservicomei.com.br"


class PixRequest(BaseModel):
    payer_name: str
    payer_cpf: str
    payer_phone: str
    payer_email: str = None


class PrewarmedPageManager:
    """
    Mantém um pool de páginas já navegadas para a URL de checkout.
    Quando uma requisição chega, a página já está carregada e pronta
    para receber os dados do formulário, eliminando o tempo de navegação.
    """

    def __init__(self, pool_size=3):
        self.playwright = None
        self.browser = None
        self.context = None
        self.pool_size = pool_size
        self.page_queue: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        self._running = False

    async def start(self):
        """Inicializa o Playwright e começa a pré-aquecer páginas."""
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--no-zygote',
                '--single-process',
                '--disable-extensions',
            ]
        )
        self.context = await self.browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        )
        self._running = True
        asyncio.create_task(self._maintain_pool())
        logger.info("PrewarmedPageManager iniciado com sucesso.")

    async def _maintain_pool(self):
        """Loop em background que mantém o pool sempre cheio."""
        while self._running:
            if self.page_queue.qsize() < self.pool_size:
                try:
                    page = await self._create_prewarmed_page()
                    await self.page_queue.put(page)
                    logger.info(f"Página pré-aquecida adicionada ao pool. Total: {self.page_queue.qsize()}")
                except Exception as e:
                    logger.error(f"Erro ao pré-aquecer página: {e}")
                    await asyncio.sleep(2)
            else:
                await asyncio.sleep(0.5)

    async def _create_prewarmed_page(self):
        """Cria uma página já navegada para o checkout."""
        page = await self.context.new_page()

        # Bloqueia recursos pesados para acelerar carregamento
        async def block_resources(route):
            if route.request.resource_type in ["image", "font", "media", "stylesheet"]:
                return await route.abort()
            url = route.request.url.lower()
            blocked_domains = [
                "facebook", "google-analytics", "hotjar",
                "clarity", "tiktok", "doubleclick", "gtag"
            ]
            if any(domain in url for domain in blocked_domains):
                return await route.abort()
            await route.continue_()

        await page.route("**/*", block_resources)

        # Navega antecipadamente - quando o usuário pedir, a página já estará pronta
        await page.goto(EXTERNAL_CHECKOUT_URL, wait_until='domcontentloaded', timeout=15000)
        
        # Aguarda o form estar disponível (máximo 5s)
        try:
            await page.wait_for_function("window.form && typeof realizarPagamento === 'function'", timeout=5000)
        except:
            pass  # Se não encontrar, a injeção JS tentará novamente

        return page

    async def get_page(self):
        """
        Retorna uma página pronta do pool.
        Se o pool estiver vazio, cria uma nova sob demanda.
        """
        try:
            page = self.page_queue.get_nowait()
            logger.info("Página obtida do pool (pré-aquecida).")
            return page
        except asyncio.QueueEmpty:
            logger.warning("Pool vazio, criando página sob demanda...")
            return await self._create_prewarmed_page()

    async def close(self):
        self._running = False
        while not self.page_queue.empty():
            page = await self.page_queue.get()
            await page.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


page_manager = PrewarmedPageManager(pool_size=3)


@app.on_event("startup")
async def startup_event():
    await page_manager.start()


@app.on_event("shutdown")
async def shutdown_event():
    await page_manager.close()


async def automate_pix_generation(data: PixRequest):
    """
    Gera PIX usando página pré-aquecida.
    Como a página já está carregada, só precisa injetar dados e submeter.
    """
    payer_email = data.payer_email
    if not payer_email:
        safe_name = ''.join(c for c in data.payer_name.lower() if c.isalpha() or c == ' ').replace(' ', '.')
        payer_email = f"{safe_name}@gmail.com"

    cpf_clean = ''.join(c for c in data.payer_cpf if c.isdigit())
    phone_clean = ''.join(c for c in data.payer_phone if c.isdigit())

    # Pega uma página que já está na URL de checkout
    page = await page_manager.get_page()
    pix_url = None
    error_msg = None
    response_received = asyncio.Event()

    try:
        async def handle_response(response):
            nonlocal pix_url, error_msg
            url = response.url
            if response.status < 400 and ('/orders' in url or '/pagamento' in url or '/checkout' in url):
                try:
                    resp_data = await response.json()
                    if 'redirect' in resp_data and resp_data['redirect']:
                        redirect = resp_data['redirect']
                        pix_url = redirect if redirect.startswith('http') else f"{EXTERNAL_BASE_URL}/{redirect.lstrip('/')}"
                        response_received.set()
                    elif 'url' in resp_data and resp_data['url']:
                        pix_url = resp_data['url']
                        response_received.set()
                    elif 'pix_url' in resp_data and resp_data['pix_url']:
                        pix_url = resp_data['pix_url']
                        response_received.set()
                    elif 'errors' in resp_data:
                        errors = resp_data['errors']
                        first_error = list(errors.values())[0]
                        error_msg = first_error[0] if isinstance(first_error, list) else str(first_error)
                        response_received.set()
                except:
                    pass

        page.on('response', handle_response)

        # Injeta os dados imediatamente - a página já está carregada!
        await page.evaluate("""async (data) => {
            return new Promise((resolve, reject) => {
                let attempts = 0;
                const checkInterval = setInterval(() => {
                    attempts++;
                    if (window.form && typeof realizarPagamento === 'function') {
                        clearInterval(checkInterval);
                        window.form.email = data.email;
                        window.form.first_name = data.name;
                        window.form.doc = data.cpf;
                        window.form.phone = data.phone;
                        window.form.postal_code = '01310-100';
                        window.form.address_line_1 = 'Avenida Paulista';
                        window.form.address_number = '1000';
                        window.form.address_neighborhood = 'Bela Vista';
                        window.form.city = 'São Paulo';
                        window.form.state = 'SP';
                        window.form.inputs_with_errors = [];
                        window.form.address_disabled = 1;
                        window.form.payment_method = 'pix_appmax';

                        const btn = document.querySelector('#general-submit-button') || document.createElement('button');
                        btn.disabled = false;
                        realizarPagamento(btn);
                        resolve('ok');
                    }
                    if (attempts > 20) {
                        clearInterval(checkInterval);
                        reject('timeout: form nao encontrado');
                    }
                }, 50);  // Intervalo menor = detecção mais rápida
            });
        }""", {
            'email': payer_email,
            'name': data.payer_name,
            'cpf': cpf_clean,
            'phone': phone_clean
        })

        # Aguarda a resposta do servidor de checkout
        try:
            await asyncio.wait_for(response_received.wait(), timeout=6.0)
        except asyncio.TimeoutError:
            # Fallbacks rápidos
            current_url = page.url
            if 'obrigado' in current_url or 'sucesso' in current_url or 'pix' in current_url:
                pix_url = current_url
            else:
                try:
                    pix_element = await page.query_selector('[data-pix-url], [data-redirect], .pix-url, .redirect-url')
                    if pix_element:
                        pix_url = await pix_element.get_attribute('href') or await pix_element.text_content()
                except:
                    pass

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Erro na geração de PIX: {e}")
    finally:
        # Fecha a página (o pool criará uma nova automaticamente em background)
        try:
            await page.close()
        except:
            pass

    return pix_url, error_msg


@app.post('/proxy/pix')
async def proxy_pix(request: PixRequest):
    """Endpoint principal para geração de PIX."""
    pix_url, error = await automate_pix_generation(request)

    if pix_url:
        logger.info(f"PIX gerado com sucesso: {pix_url}")
        return JSONResponse({
            'success': True,
            'pixUrl': pix_url,
            'redirectUrl': pix_url
        })
    else:
        logger.error(f"Falha ao gerar PIX: {error}")
        return JSONResponse({
            'success': False,
            'error': error or 'Erro ao gerar PIX',
            'message': 'Não foi possível gerar o PIX. Tente novamente.'
        }, status_code=400)


@app.get('/')
async def index():
    return FileResponse('static/index.html')


@app.get('/health')
async def health():
    return {"status": "ok"}


if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Iniciando servidor na porta {port}")
    uvicorn.run(app, host='0.0.0.0', port=port)
