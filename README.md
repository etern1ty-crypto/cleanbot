<img src="https://capsule-render.vercel.app/api?type=waving&color=0:1a1b27,50:2ECC71,100:1a1b27&height=200&section=header&text=CleanBot&fontSize=50&fontColor=FFFFFF&fontAlignY=35&desc=Telegram%20Bot%20%2B%20Mini%20App%20%E2%80%94%20Moderation%20%26%20Automation&descSize=16&descColor=ABEBC6&descAlignY=55&animation=fadeIn" width="100%"/>

<div align="center">

[![Python 3.9+](https://img.shields.io/badge/python-3.9+-3670A0?style=flat-square&logo=python&logoColor=ffdd54)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Telegram Bot](https://img.shields.io/badge/telegram-bot-2CA5E0?style=flat-square&logo=telegram&logoColor=white)]()
[![TMA](https://img.shields.io/badge/TMA-mini_app-26A5E4?style=flat-square&logo=telegram)]()
[![SQLite](https://img.shields.io/badge/sqlite-database-003B57?style=flat-square&logo=sqlite&logoColor=white)]()

**🇷🇺 [Русский](#-описание) · 🇬🇧 [English](#-overview)**

</div>

---

## 🇬🇧 Overview

**CleanBot** is a Telegram bot with TMA (Telegram Mini App) integration for group chat moderation and content management automation.

### Features

- **Group Moderation** — automated content filtering, user management, and rule enforcement
- **Telegram Mini App (TMA)** — web-based interface for bot configuration and management
- **SQLite Storage** — persistent data storage with rotating log files
- **Async Architecture** — built on `asyncio` for high-performance event handling

### Project Structure

```
cleanbot/
├── bot.py              # Main bot logic — moderation, handlers, commands
├── bot_tma.py          # Telegram Mini App bot integration
├── tma_server.py       # TMA web server
├── requirements.txt    # Python dependencies
└── README.md
```

### Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure
export TELEGRAM_BOT_TOKEN="your-bot-token"

# Run the bot
python bot.py

# Run the TMA server (separate terminal)
python tma_server.py
```

### Tech Stack

![Python](https://img.shields.io/badge/python-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)
![Telegram](https://img.shields.io/badge/telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)
![SQLite](https://img.shields.io/badge/sqlite-003B57?style=for-the-badge&logo=sqlite&logoColor=white)

---

## 🇷🇺 Описание

**CleanBot** — Telegram-бот с интеграцией TMA (Telegram Mini App) для модерации групповых чатов и автоматизации управления контентом.

### Возможности

- **Модерация групп** — автоматическая фильтрация контента, управление пользователями
- **Telegram Mini App (TMA)** — веб-интерфейс для настройки и управления ботом
- **SQLite хранилище** — постоянное хранение данных с ротацией логов
- **Async архитектура** — построен на `asyncio` для высокопроизводительной обработки

### Быстрый Старт

```bash
# Установка зависимостей
pip install -r requirements.txt

# Настройка
export TELEGRAM_BOT_TOKEN="ваш-токен-бота"

# Запуск бота
python bot.py

# Запуск TMA сервера (отдельный терминал)
python tma_server.py
```

---

<div align="center">

### License

MIT — see [LICENSE](LICENSE) for details.

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:1a1b27,50:2ECC71,100:1a1b27&height=80&section=footer" width="100%"/>

</div>
