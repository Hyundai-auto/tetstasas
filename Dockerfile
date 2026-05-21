# Use a imagem oficial do Python com Playwright pré-instalado
FROM mcr.microsoft.com/playwright/python:v1.43.0-jammy

# Definir diretório de trabalho
WORKDIR /app

# Copiar arquivos de requisitos e instalar dependências
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar o restante do código
COPY . .

# O Playwright já vem com os navegadores na imagem da Microsoft,
# mas garantimos que o Chromium está pronto
RUN playwright install chromium

# Expor a porta que a aplicação usa
EXPOSE 5000

# Comando para iniciar a aplicação com Uvicorn
# --workers 1 porque o Playwright compartilha estado em memória (pool de páginas)
# Para escalar, use múltiplas instâncias do container
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "5000", "--timeout-keep-alive", "30"]
