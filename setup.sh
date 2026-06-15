#!/bin/bash
# ============================================
# Fachio - Media Relay Downloader
# ONE CLICK SETUP
# ============================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

BOT_FILE="bot.py"
REQ_FILE="requirements.txt"
SESSION="bot"

echo -e "${BLUE}════════════════════════════════════════${NC}"
echo -e "${GREEN}🔀 Fachio - Media Relay Downloader | One Click Setup${NC}"
echo -e "${BLUE}════════════════════════════════════════${NC}"
echo ""

# [1/5] Cek file bot
echo -e "${YELLOW}[1/5] Mengecek file ${BOT_FILE}...${NC}"
if [ ! -f "$BOT_FILE" ]; then
    echo -e "${RED}❌ Error: File ${BOT_FILE} tidak ditemukan!${NC}"
    exit 1
fi
echo -e "${GREEN}✅ File ${BOT_FILE} ditemukan${NC}"

# [2/5] Konfigurasi token
echo -e "${YELLOW}[2/5] Konfigurasi token bot...${NC}"
BOT_TOKEN=""
if grep -q "YOUR_BOT_TOKEN_HERE" "$BOT_FILE"; then
    echo -e "${YELLOW}⚠️  Token bot masih default!${NC}"
    echo ""
    echo "📌 Cara dapatkan token:"
    echo "   1. Buka Telegram → cari @BotFather"
    echo "   2. Kirim /newbot, ikuti instruksi hingga dapat token"
    echo ""
    read -p "🔑 Masukkan token bot: " BOT_TOKEN
    if [ -z "$BOT_TOKEN" ]; then
        echo -e "${RED}❌ Token tidak boleh kosong!${NC}"
        exit 1
    fi
    cp "$BOT_FILE" "${BOT_FILE}.bak"
    sed -i "s|YOUR_BOT_TOKEN_HERE|${BOT_TOKEN}|g" "$BOT_FILE"
    echo -e "${GREEN}✅ Token berhasil diisi${NC}"
else
    echo -e "${GREEN}✅ Token sudah dikonfigurasi${NC}"
fi

# [3/5] Install dependensi
echo -e "${YELLOW}[3/5] Install dependensi...${NC}"
sudo apt update -qq
sudo apt install -y python3 python3-pip ffmpeg tmux
if [ -f "$REQ_FILE" ]; then
    pip3 install -r "$REQ_FILE" --break-system-packages -q
else
    echo -e "${YELLOW}⚠️  ${REQ_FILE} tidak ada, memasang paket default...${NC}"
    pip3 install python-telegram-bot yt-dlp httpx --break-system-packages -q
fi
echo -e "${GREEN}✅ Dependensi terinstall${NC}"

# [4/5] Verifikasi token (hanya jika baru diinput)
echo -e "${YELLOW}[4/5] Verifikasi token...${NC}"
if [ -n "$BOT_TOKEN" ]; then
    RESPONSE=$(curl -s "https://api.telegram.org/bot${BOT_TOKEN}/getMe" || true)
    if echo "$RESPONSE" | grep -q '"ok":true'; then
        echo -e "${GREEN}✅ Token valid! Bot terhubung ke Telegram.${NC}"
    else
        echo -e "${RED}⚠️  Token tidak valid! Cek kembali.${NC}"
        read -p "Lanjutkan? (y/n): " CONTINUE
        if [[ ! "$CONTINUE" =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
else
    echo -e "${YELLOW}ℹ️  Token sudah tertanam di ${BOT_FILE}, verifikasi dilewati.${NC}"
fi

# [5/5] Jalankan bot di background (tmux)
echo -e "${YELLOW}[5/5] Menjalankan bot di background (tmux)...${NC}"
if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo -e "${YELLOW}⚠️  Sesi '${SESSION}' sudah ada, menghentikan yang lama...${NC}"
    tmux kill-session -t "$SESSION"
fi
tmux new-session -d -s "$SESSION" "python3 $BOT_FILE"
echo -e "${BLUE}════════════════════════════════════════${NC}"
echo -e "${GREEN}✅ Setup selesai! Fachio berjalan di background.${NC}"
echo ""
echo "📌 Lihat bot    : tmux attach -t ${SESSION}"
echo "📌 Keluar lagi  : Ctrl+B lalu D"
echo "📌 Cek sesi     : tmux ls"
echo "📌 Hentikan bot : tmux kill-session -t ${SESSION}"
echo -e "${BLUE}════════════════════════════════════════${NC}"
