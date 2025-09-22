
# DevOps Test Task

Документ описывает **как именно было сделано** тестовое задание: контейнеризация приложения (Docker), обратный прокси (Nginx), автоматический деплой (Jenkins), масштабирование и **zero‑downtime** обновления.

---

## Содержание
1. [Требования](#требования)
2. [Структура проекта](#структура-проекта)
3. [Docker: как устроен образ приложения](#docker-как-устроен-образ-приложения)
4. [Docker Compose: как устроен стек](#docker-compose-как-устроен-стек)
5. [Nginx: схема проксирования](#nginx-схема-проксирования)
6. [Локальный запуск](#локальный-запуск)
7. [CI/CD на Jenkins: как устроен пайплайн](#cicd-на-jenkins-как-устроен-пайплайн)
8. [Zero‑downtime деплой](#zero-downtime-деплой)
9. [Логирование и мониторинг](#логирование-и-мониторинг)
10. [Традиционные проблемы и решения (FAQ)](#традиционные-проблемы-и-решения-faq)
11. [Планы на улучшения](#планы-на-улучшения)

---

## Требования
- Linux (проверено на Debian 12)
- Docker (Engine) и Docker Compose v2
- Git
- Порт **80** свободен на хосте (или заменить на другой в `compose`)

Проверка:
```bash
docker --version
docker compose version
git --version
```

---

## Структура проекта

```
devops-test/
├─ app/
│  ├─ Dockerfile
│  ├─ app.py
│  └─ requirements.txt
├─ nginx/
│  ├─ Dockerfile
│  └─ default.conf
├─ docker-compose.yml
├─ .env                 # создаётся при первом запуске/деплое
└─ Jenkinsfile          # вариант с хранением пайплайна в репо
```

Код лежит в репозитории: `https://github.com/percyvelle1/test-task` в каталоге `devops-test/`.

---

## Docker: как устроен образ приложения

Образ приложения собирается на базе `python:3.12-slim`. Используется двухстейджевый билд для ускорения сборки и меньшего размера образа:

1. **build-стейдж** — качаются зависимости в виде wheel-пакетов;
2. **runtime-стейдж** — устанавливаются wheel’ы и копируется код приложения.

Ключевые шаги (фрагмент `app/Dockerfile`):
```dockerfile
FROM python:3.12-slim AS build
WORKDIR /app
RUN pip install --no-cache-dir --upgrade pip
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps -r requirements.txt -w /wheels

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY app.py .
CMD ["python", "app.py"]
```

Приложение — минимальный Flask, отвечающий `Hello, World!` (или изменённый текст).

---

## Docker Compose: как устроен стек

В `docker-compose.yml` описаны два сервиса: приложение `app` и обратный прокси `nginx`. Логи Nginx вынесены в volume, оба сервиса в одной сети `web`. Для `app` добавлен **healthcheck**, чтобы Nginx не слал трафик в ещё неготовый контейнер.

```yaml
name: devops-test
services:
  app:
    build: ./app
    env_file: .env
    restart: unless-stopped
    networks: [web]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:3000"]
      interval: 5s
      timeout: 3s
      retries: 5
      start_period: 5s

  nginx:
    build: ./nginx
    depends_on:
      - app
    ports:
      - "80:80"
    volumes:
      - nginx_logs:/var/log/nginx
    restart: unless-stopped
    networks: [web]

volumes:
  nginx_logs:

networks:
  web:
    driver: bridge
```

> В `.env` лежат неболезненные параметры (порт/режим отладки). Для тестового задания допускается хранить `.env` в репозитории. В проде — лучше секреты хранить вне гита.

Пример `.env`:
```env
PORT=3000
DEBUG=1
```

---

## Nginx: схема проксирования

Файл `nginx/default.conf` проксирует весь HTTP-трафик на приложение и содержит настройки для более «мягкого» поведения при рестартах (избегаем 502):

```nginx
upstream app_upstream {
    server app:3000 max_fails=3 fail_timeout=5s;
    keepalive 32;
}

server {
    listen 80;
    server_name _;

    access_log /var/log/nginx/access.log main;
    error_log  /var/log/nginx/error.log warn;

    location /healthz {
        return 200 "nginx ok\n";
        add_header Content-Type text/plain;
    }

    location / {
        proxy_pass http://app_upstream;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;

        # Быстрая переотправка запроса на другой инстанс при ошибке
        proxy_next_upstream error timeout http_502 http_503 http_504;
    }
}
```

---

## Локальный запуск

```bash
git clone https://github.com/percyvelle1/test-task.git
cd test-task/devops-test

# 1) .env (если не существует)
[ -f .env ] || (echo "PORT=3000" > .env && echo "DEBUG=1" >> .env)

# 2) старт сервисов
docker compose up -d --build

# 3) проверка
curl http://localhost
curl http://localhost/healthz
```

Остановить/перезапустить:
```bash
docker compose ps
docker compose logs -f
docker compose restart
docker compose down
```

---

## CI/CD на Jenkins: как устроен пайплайн

### Что сделано
- Jenkins подключён к GitHub: **webhook** вызывает билд при `push` в `main`.
- В Jenkins настроен Pipeline (вариант «Pipeline script from SCM»).
- Деплой идёт на удалённый сервер по **SSH** (пользователь `debian`).
- SSH‑ключ хранится в Jenkins Credentials (`github-percyvellew-credentials`).

### Логика пайплайна
1. Проверить/обновить каталог проекта на сервере.
2. Подтянуть свежий код (`git pull` или жёстко `fetch+reset`).
3. Пересобрать и обновить `app` c масштабированием `--scale app=2` и **rolling‑update**.
4. Сделать health‑проверку через Nginx.

Фрагмент Jenkinsfile:
```groovy
pipeline {
  agent any
  stages {
    stage('Build & Deploy') {
      steps {
        sshagent(['github-percyvellew-credentials']) {
          sh '''
            ssh -o StrictHostKeyChecking=no debian@<SERVER_IP> "
              set -e
              if [ ! -d ~/devops-test/.git ]; then
                rm -rf ~/devops-test
                git clone https://github.com/percyvelle1/test-task.git ~/devops-test
              fi
              cd ~/devops-test/devops-test
              git fetch origin main && git reset --hard origin/main

              docker compose up -d --scale app=2 --no-deps --build app
              sleep 5
              curl -fsS http://localhost/healthz
            "
          '''
        }
      }
    }
  }
}
```

> При первом запуске можно руками выполнить `docker compose up -d --scale app=2` — далее пайплайн всегда поддерживает две реплики и обновляет их поочерёдно.

### Вебхук GitHub
- **Payload URL**: `https://<jenkins-host>/github-webhook/`
- **Content type**: `application/json`
- События: `Just the push event`

---

## Zero‑downtime деплой

Используется стратегия «**две реплики + поочередный рестарт**»:
- `docker compose up -d --scale app=2` — держим **минимум 2 инстанса** приложения;
- `--no-deps --build app` — пересобираем только `app`, Nginx остаётся работать;
- Nginx при ошибках быстро перекидывает запросы на живой инстанс (`proxy_next_upstream`).

### Как проверить руками
В одном терминале:
```bash
watch -n 1 curl -s http://<SERVER_IP>
```
В другом сделайте push (или руками выполните обновление образа). В выводе `watch` не должно быть 502/timeout — текст ответа просто сменится со старой версии на новую.

---

## Логирование и мониторинг

### Логи
- Приложение:
  ```bash
  docker compose logs -f app
  ```
- Nginx:
  ```bash
  docker compose logs -f nginx
  # файлы внутри контейнера → /var/log/nginx/
  # вынесены в volume → devops-test_nginx_logs
  ```

### Ротация логов Docker (опционально, на хосте)
`/etc/docker/daemon.json`:
```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "5" }
}
```
```bash
sudo systemctl restart docker
```

### Мониторинг (опционально)
- Базовый мониторинг VM/хоста — встроенные средства ОС/облака.
- Расширенный — `prom/node-exporter` + Prometheus + Grafana.

---

## Традиционные проблемы и решения (FAQ)

**Порт 80 занят / address already in use**  
На хосте уже работает системный nginx/apache. Либо остановить сервис,
```bash
sudo systemctl stop nginx && sudo systemctl disable nginx
```
либо сменить проброс порта на `8080:80` в `docker-compose.yml`.

**`no such service: ...` при `--scale`**  
Нужно указывать **имя сервиса** из `docker-compose.yml` (у нас — `app`), а не имя контейнера.
Также `--scale` работает **только без** `container_name`.

**`no configuration file provided`**  
Команда выполняется не в каталоге, где лежит `docker-compose.yml`. Перейдите в `devops-test/devops-test`.

**`env file ... .env not found`**  
Создайте `.env` рядом с `docker-compose.yml` или удалите `env_file` из compose.

**`fatal: need to specify how to reconcile divergent branches`**  
Вместо `git pull` используем «чистое» обновление:
```bash
git fetch origin main && git reset --hard origin/main
```

**502 во время деплоя**  
Помогают: healthcheck в `compose`, `proxy_next_upstream` в Nginx, задержка `sleep 5` и/или обновление по одному инстансу.

**Jenkins тянет `master`, а у нас `main`**  
В настройках SCM укажите ветку `*/main` и правильный путь к Jenkinsfile (например, `devops-test/Jenkinsfile`).

---

## Планы на улучшения
- Хранить образы в реестре (GHCR/Docker Hub), деплой через `docker compose pull`.
- Blue‑Green деплой (два стека и переключение трафика).
- Автоматическая проверка `/healthz` каждого инстанса и вывод метрик в Prometheus.
- Тераформ/Ansible для развёртывания хоста.
