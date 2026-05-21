import os
import asyncio
import logging
from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
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

class PrewarmedPageManager:
    def __init__(self, pool_size=3):
        self.playwright = None
        self.browser = None
        self.context = None
        self.pool_size = pool_size
        self.page_queue: asyncio.Queue = asyncio.Queue(maxsize=pool_size)
        self._running = False

    async def start(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage',
                '--disable-gpu', '--no-zygote', '--single-process', '--disable-extensions',
            ]
        )
        self.context = await self.browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
        )
        self._running = True
        asyncio.create_task(self._maintain_pool())
        logger.info("PrewarmedPageManager iniciado")

    async def _maintain_pool(self):
        while self._running:
            if self.page_queue.qsize() < self.pool_size:
                try:
                    page = await self._create_prewarmed_page()
                    await self.page_queue.put(page)
                except Exception as e:
                    logger.error(f"Erro ao pré-aquecer: {e}")
                    await asyncio.sleep(2)
            else:
                await asyncio.sleep(0.5)

    async def _create_prewarmed_page(self):
        page = await self.context.new_page()
        async def block_resources(route):
            if route.request.resource_type in ["image", "font", "media", "stylesheet"]:
                return await route.abort()
            url = route.request.url.lower()
            if any(domain in url for domain in ["facebook", "google-analytics", "hotjar", "clarity", "tiktok", "doubleclick", "gtag"]):
                return await route.abort()
            await route.continue_()

        await page.route("**/*", block_resources)
        await page.goto(EXTERNAL_CHECKOUT_URL, wait_until='domcontentloaded')
        
        # Espera as variáveis essenciais carregarem (csrf e cart_token)
        try:
            await page.wait_for_function("window.ck && window.ck.data && window.ck.data.cart_token && document.querySelector('input[name=\"_token\"]')", timeout=10000)
        except:
            pass

        return page

    async def get_page(self):
        try:
            return self.page_queue.get_nowait()
        except asyncio.QueueEmpty:
            return await self._create_prewarmed_page()

    async def close(self):
        self._running = False
        while not self.page_queue.empty():
            page = await self.page_queue.get()
            await page.close()
        if self.browser: await self.browser.close()
        if self.playwright: await self.playwright.stop()

page_manager = PrewarmedPageManager(pool_size=3)

@app.on_event("startup")
async def startup_event():
    await page_manager.start()

@app.on_event("shutdown")
async def shutdown_event():
    await page_manager.close()

async def automate_pix_generation(data: PixRequest):
    payer_email = data.payer_email or f"{''.join(c for c in data.payer_name.lower() if c.isalpha() or c == ' ').replace(' ', '.')}@gmail.com"
    cpf_clean = ''.join(c for c in data.payer_cpf if c.isdigit())
    phone_clean = ''.join(c for c in data.payer_phone if c.isdigit())

    page = await page_manager.get_page()
    
    try:
        # Em vez de preencher UI e clicar, chamamos a API interna diretamente via JS!
        # Isso pula todas as animações e lógicas pesadas do frontend.
        result = await page.evaluate("""async (data) => {
            try {
                const csrf = document.querySelector('input[name="_token"]').value;
                const cartToken = window.ck.data.cart_token;
                
                const payload = {
                    inputs_with_errors: [],
                    cart_token: cartToken,
                    payment_method: 'pix_appmax',
                    email: data.email,
                    first_name: data.name,
                    doc: data.cpf,
                    phone: data.phone,
                    postal_code: '01310-100',
                    address_line_1: 'Avenida Paulista',
                    address_number: '1000',
                    address_neighborhood: 'Bela Vista',
                    city: 'São Paulo',
                    state: 'SP',
                    address_disabled: 1,
                    opt_in: true,
                    is_province: false
                };

                const response = await fetch('/orders', {
                    method: 'POST',
                    headers: {
                        'content-type': 'application/json',
                        'accept': 'application/json',
                        'X-CSRF-TOKEN': csrf
                    },
                    body: JSON.stringify(payload)
                });
                
                const json = await response.json();
                return { success: true, data: json };
            } catch (e) {
                return { success: false, error: e.toString() };
            }
        }""", {
            'email': payer_email,
            'name': data.payer_name,
            'cpf': cpf_clean,
            'phone': phone_clean
        })

        if result.get('success') and result.get('data'):
            resp_data = result['data']
            if 'redirect' in resp_data and resp_data['redirect']:
                redirect = resp_data['redirect']
                pix_url = redirect if redirect.startswith('http') else f"{EXTERNAL_BASE_URL}/{redirect.lstrip('/')}"
                return pix_url, None
            elif 'errors' in resp_data:
                errors = resp_data['errors']
                first_error = list(errors.values())[0]
                return None, first_error[0] if isinstance(first_error, list) else str(first_error)
            
        return None, "Resposta inválida da API"

    except Exception as e:
        return None, str(e)
    finally:
        asyncio.create_task(page.close())

@app.post('/proxy/pix')
async def proxy_pix(request: PixRequest):
    pix_url, error = await automate_pix_generation(request)
    if pix_url:
        return JSONResponse({'success': True, 'pixUrl': pix_url, 'redirectUrl': pix_url})
    return JSONResponse({'success': False, 'error': error or 'Erro ao gerar PIX', 'message': 'Tente novamente.'}, status_code=400)

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
