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

# Expor a porta que o Flask usa
EXPOSE 5000

# Usar Gunicorn em produção para melhor performance
# Instalar Gunicorn e gevent para workers assíncronos
RUN pip install --no-cache-dir gunicorn gevent

# Comando para iniciar a aplicação com Gunicorn
# 4 workers gevent = melhor throughput para requisições paralelas com I/O bound
# timeout 15s = suficiente para geração de PIX otimizada
CMD ["gunicorn", "-k", "gevent", "-w", "4", "-b", "0.0.0.0:5000", "--timeout", "15", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
