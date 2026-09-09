# 部署

## 前台运行

```bash
python main.py
```

适合开发调试。

## 后台运行（Windows）

### 方法一：pythonw

```bash
pythonw main.py
```

关闭终端窗口后继续运行。

### 方法二：nssm 服务

```bash
nssm install ZCBOT "C:\Python314\python.exe" "E:\工程\机器人\zgric_onebot11_35xLe\main.py"
nssm start ZCBOT
```

## 后台运行（Linux）

### systemd

```bash
sudo nano /etc/systemd/system/zcbot.service
```

```ini
[Unit]
Description=ZCBOT Framework
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/zcbot
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable zcbot
sudo systemctl start zcbot
sudo systemctl status zcbot
```

## Docker 部署

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
```

### docker-compose.yml

```yaml
version: '3.8'
services:
  zcbot:
    build: .
    container_name: zcbot
    restart: unless-stopped
    ports:
      - "6830:6830"
      - "8080:8080"
    volumes:
      - ./config.yaml:/app/config.yaml
      - ./data:/app/data
      - ./plugins:/app/plugins
    environment:
      - PYTHONUNBUFFERED=1

  napcat:
    image: napcat/napcat:latest
    container_name: napcat
    restart: unless-stopped
    ports:
      - "3000:3000"
    volumes:
      - ./napcat/config:/app/config
```

```bash
docker-compose up -d
```

## 开机自启（Windows）

```powershell
schtasks /create /tn "ZCBOT" /tr "pythonw E:\工程\机器人\zgric_onebot11_35xLe\main.py" /sc onlogon
```
