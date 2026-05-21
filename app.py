import os
import asyncio
import logging
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright

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


class BrowserManager:
    """
    Gerencia o browser Playwright com pool de páginas pré-aquecidas.
    Usa abordagem híbrida: Playwright para bypass da Cloudflare + fetch direto para velocidade.
    """

    def __init__(self, pool_size=2):
        self.playwright = None
        self.browser = None
        self.pool_size = pool_size
        self.page_queue: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        self._running = False
        self._lock = asyncio.Lock()

    async def start(self):
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
        self._running = True
        asyncio.create_task(self._maintain_pool())
        logger.info("BrowserManager iniciado com sucesso")

    async def _maintain_pool(self):
        """Mantém o pool de páginas pré-aquecidas."""
        while self._running:
            if self.page_queue.qsize() < self.pool_size:
                try:
                    page = await self._create_ready_page()
                    if page:
                        await self.page_queue.put(page)
                        logger.info(f"Página pronta adicionada ao pool ({self.page_queue.qsize()}/{self.pool_size})")
                except Exception as e:
                    logger.error(f"Erro ao criar página: {e}")
                    await asyncio.sleep(3)
            else:
                await asyncio.sleep(1)

    async def _create_ready_page(self):
        """Cria uma nova página já navegada para o checkout."""
        context = await self.browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        )
        page = await context.new_page()

        # Bloqueia recursos pesados
        async def block_resources(route):
            if route.request.resource_type in ["image", "font", "media"]:
                return await route.abort()
            url = route.request.url.lower()
            if any(d in url for d in ["facebook", "google-analytics", "hotjar", "clarity", "tiktok", "doubleclick", "gtag"]):
                return await route.abort()
            await route.continue_()

        await page.route("**/*", block_resources)

        # Navega para o checkout
        try:
            await page.goto(EXTERNAL_CHECKOUT_URL, wait_until='domcontentloaded', timeout=20000)
        except Exception as e:
            logger.warning(f"Timeout na navegação (pode ser normal): {e}")

        # Aguarda o carregamento das variáveis essenciais
        try:
            await page.wait_for_function(
                "window.ck && window.ck.data && window.ck.data.cart_token && document.querySelector('input[name=\"_token\"]')",
                timeout=15000
            )
            logger.info("Página carregada com sucesso - CSRF e cart_token disponíveis")
        except Exception as e:
            logger.warning(f"Variáveis não encontradas no tempo limite: {e}")
            # Tenta esperar mais um pouco
            await asyncio.sleep(2)

        return page

    async def get_page(self):
        """Obtém uma página pronta do pool ou cria uma nova."""
        try:
            page = self.page_queue.get_nowait()
            logger.info("Página obtida do pool")
            return page
        except asyncio.QueueEmpty:
            logger.warning("Pool vazio, criando página sob demanda...")
            return await self._create_ready_page()

    async def close(self):
        self._running = False
        while not self.page_queue.empty():
            page = await self.page_queue.get()
            try:
                await page.context.close()
            except:
                pass
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


browser_manager = BrowserManager(pool_size=2)


@app.on_event("startup")
async def startup_event():
    await browser_manager.start()


@app.on_event("shutdown")
async def shutdown_event():
    await browser_manager.close()


async def automate_pix_generation(data: PixRequest):
    """
    Gera PIX usando abordagem híbrida:
    - Playwright para contornar Cloudflare
    - fetch() direto no JS para máxima velocidade
    - Fallback para método tradicional se o fetch direto falhar
    """
    payer_email = data.payer_email
    if not payer_email:
        safe_name = ''.join(c for c in data.payer_name.lower() if c.isalpha() or c == ' ').replace(' ', '.')
        payer_email = f"{safe_name}@gmail.com"

    cpf_clean = ''.join(c for c in data.payer_cpf if c.isdigit())
    phone_clean = ''.join(c for c in data.payer_phone if c.isdigit())

    page = await browser_manager.get_page()

    try:
        # ===== MÉTODO 1: Fetch direto (mais rápido) =====
        logger.info("Tentando método rápido (fetch direto)...")
        
        result = await page.evaluate("""async (data) => {
            try {
                // Verifica se as variáveis estão disponíveis
                const csrfEl = document.querySelector('input[name="_token"]');
                if (!csrfEl) return { success: false, error: 'CSRF token não encontrado na página' };
                
                const csrf = csrfEl.value;
                const cartToken = window.ck && window.ck.data ? window.ck.data.cart_token : null;
                if (!cartToken) return { success: false, error: 'cart_token não encontrado' };

                const payload = {
                    inputs_with_errors: [],
                    cart_token: cartToken,
                    payment_method: 'pix_appmax',
                    email: data.email,
                    first_name: data.name,
                    doc: data.cpf,
                    phone: data.phone,
                    postal_code: '01310100',
                    address_line_1: 'Avenida Paulista',
                    address_number: '1000',
                    address_neighborhood: 'Bela Vista',
                    city: 'São Paulo',
                    state: 'SP',
                    address_disabled: 1,
                    opt_in: true,
                    is_province: false,
                    card_installments: '1'
                };

                const response = await fetch('/orders', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-CSRF-TOKEN': csrf
                    },
                    body: JSON.stringify(payload)
                });

                const json = await response.json();
                return { success: true, status: response.status, data: json };
            } catch (e) {
                return { success: false, error: e.toString() };
            }
        }""", {
            'email': payer_email,
            'name': data.payer_name,
            'cpf': cpf_clean,
            'phone': phone_clean
        })

        logger.info(f"Resultado do fetch direto: {result}")

        if result and result.get('success') and result.get('data'):
            resp_data = result['data']
            
            if resp_data.get('redirect'):
                redirect = resp_data['redirect']
                pix_url = redirect if redirect.startswith('http') else f"{EXTERNAL_BASE_URL}/{redirect.lstrip('/')}"
                logger.info(f"PIX gerado com sucesso (método rápido): {pix_url}")
                return pix_url, None
            elif resp_data.get('url'):
                logger.info(f"PIX URL: {resp_data['url']}")
                return resp_data['url'], None
            elif resp_data.get('errors'):
                errors = resp_data['errors']
                first_key = list(errors.keys())[0]
                first_error = errors[first_key]
                error_msg = first_error[0] if isinstance(first_error, list) else str(first_error)
                logger.warning(f"Erro retornado pela API: {error_msg}")
                return None, error_msg

        # ===== MÉTODO 2: Fallback via realizarPagamento (mais lento, mas confiável) =====
        logger.info("Método rápido falhou, tentando fallback via realizarPagamento...")
        
        pix_url = None
        error_msg = None
        response_received = asyncio.Event()

        async def handle_response(response):
            nonlocal pix_url, error_msg
            url = response.url
            if response.status < 400 and ('/orders' in url or '/pagamento' in url or '/checkout' in url):
                try:
                    resp_data = await response.json()
                    if resp_data.get('redirect'):
                        redirect = resp_data['redirect']
                        pix_url = redirect if redirect.startswith('http') else f"{EXTERNAL_BASE_URL}/{redirect.lstrip('/')}"
                        response_received.set()
                    elif resp_data.get('url'):
                        pix_url = resp_data['url']
                        response_received.set()
                    elif resp_data.get('errors'):
                        errors = resp_data['errors']
                        first_key = list(errors.keys())[0]
                        first_error = errors[first_key]
                        error_msg = first_error[0] if isinstance(first_error, list) else str(first_error)
                        response_received.set()
                except:
                    pass

        page.on('response', handle_response)

        # Recarrega a página para ter um estado limpo
        try:
            await page.goto(EXTERNAL_CHECKOUT_URL, wait_until='domcontentloaded', timeout=15000)
            await page.wait_for_function("window.form && typeof realizarPagamento === 'function'", timeout=10000)
        except:
            pass

        await page.evaluate("""(data) => {
            window.form.email = data.email;
            window.form.first_name = data.name;
            window.form.doc = data.cpf;
            window.form.phone = data.phone;
            window.form.postal_code = '01310100';
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
        }""", {
            'email': payer_email,
            'name': data.payer_name,
            'cpf': cpf_clean,
            'phone': phone_clean
        })

        try:
            await asyncio.wait_for(response_received.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            current_url = page.url
            if 'obrigado' in current_url or 'sucesso' in current_url or 'pix' in current_url:
                pix_url = current_url

        if pix_url:
            logger.info(f"PIX gerado com sucesso (fallback): {pix_url}")
        else:
            logger.error(f"Falha em ambos os métodos. Erro: {error_msg}")

        return pix_url, error_msg

    except Exception as e:
        logger.error(f"Erro geral na geração de PIX: {e}", exc_info=True)
        return None, str(e)
    finally:
        try:
            await page.context.close()
        except:
            pass


@app.post('/proxy/pix')
async def proxy_pix(request: PixRequest):
    """Endpoint para geração de PIX."""
    logger.info(f"Requisição recebida: {request.payer_name} / {request.payer_cpf}")
    pix_url, error = await automate_pix_generation(request)
    if pix_url:
        return JSONResponse({'success': True, 'pixUrl': pix_url, 'redirectUrl': pix_url})
    return JSONResponse(
        {'success': False, 'error': error or 'Erro ao gerar PIX', 'message': 'Não foi possível gerar o PIX. Tente novamente.'},
        status_code=400
    )


@app.get('/health')
async def health():
    return {"status": "ok"}


@app.get('/')
async def index():
    return FileResponse(Path(__file__).parent / 'static' / 'index.html')


# Montar arquivos estáticos
static_dir = Path(__file__).parent / 'static'
if static_dir.exists():
    app.mount('/static', StaticFiles(directory=str(static_dir)), name='static')


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
