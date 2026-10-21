<div align="center">

<img src="https://i.pinimg.com/originals/d4/ea/4e/d4ea4e0c89ef8de3cc438f52db514782.gif" alt="Joker" width="35%" />

# Joker Discord Bot

![Python](https://img.shields.io/badge/Python-3.11+-3776AB.svg?style=flat-square&logo=python&logoColor=white)
![discord.py](https://img.shields.io/badge/discord.py-v2.6+-5865F2.svg?style=flat-square&logo=discord&logoColor=white)
![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg?style=flat-square)

*Discord bot chủ đề Joker (Persona 5).*

</div>

---

## Chức năng

| Nhóm | Lệnh | Chức năng |
| :--- | :--- | :--- |
| **Valorant** | `/shop`, `/nightmarket`, `/skins` | Xem cửa hàng, Night Market và xuất ảnh kho skin. |
| **Tiện ích** | `/quest`, `/hypesquad`, `/help` | Tiện ích Discord và danh sách lệnh. |

## Yêu cầu
- Python **3.11+**, `discord.py` **2.6+**
- Discord Bot Token (không cần bật Privileged Intents)
- Giữ thư mục `assets/fonts/` để tạo ảnh `/skins`

## Cài đặt

Tạo file `.env` ở thư mục gốc:

```env
BOT_TOKEN=your_discord_bot_token_here
# Tuỳ chọn: khoá mã hoá phiên Valorant
VALORANT_SECRET_KEY=your_secret_key
```

Chạy bot:

```bash
pip install -r requirements.txt
python main.py
```

Nếu không đặt `VALORANT_SECRET_KEY`, bot tạo `valorant.key` khi cần. **Hãy sao lưu khoá này**; mất hoặc đổi khoá sẽ khiến phiên Valorant đã lưu không thể giải mã. `proxies.txt` là tuỳ chọn cho `/quest`.

## Xử lý sự cố
- **Không thấy slash command:** chờ đồng bộ và kiểm tra scope `applications.commands`.
- **Lỗi Valorant:** giữ nguyên `valorant.key`/`VALORANT_SECRET_KEY` và dùng link Riot còn hạn.
- **Ảnh skin/emoji lỗi:** kiểm tra `assets/fonts/`, kết nối `valorant-api.com` và quyền **Use External Emojis**.

## Giấy phép và ghi công
Phát hành theo **GNU General Public License v3.0 (GPL-3.0)**, xem [LICENSE](LICENSE).