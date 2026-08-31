import os
import re
import json
import base64
import logging
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

def load_e_database():
    try:
        with open("e_additives.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logging.error("Файл e_additives.json не найден!")
        return {}

E_DB = load_e_database()

class AnalysisState(StatesGroup):
    waiting_for_photo = State()

def find_e_additives_in_text(text: str) -> list:
    matches = re.findall(r'\b[eе]\s?(\d{3,4})\b', text, re.IGNORECASE)
    found = []
    for match in matches:
        e_code = f"E{match.upper()}"
        if e_code in E_DB:
            found.append({"code": e_code, "info": E_DB[e_code]})
        else:
            found.append({"code": e_code, "info": {"name": "Неизвестная добавка", "danger": "Неизвестно", "description": "Отсутствует в локальной базе."}})
    return found

async def analyze_with_openrouter(image_bytes: bytes, local_db_report: str, extracted_text: str) -> str:
    image_base64 = base64.b64encode(image_bytes).decode('utf-8')
    
    system_prompt = """Ты — эксперт по безопасности пищевых продуктов. 
    Проанализируй состав продукта. 
    1. Учитывай извлеченный текст ингредиентов.
    2. ОБЯЗАТЕЛЬНО учти предварительный отчет из локальной базы (он уже содержит проверенные опасные E-добавки).
    3. Если есть другие опасные компоненты (не только E), укажи их.
    4. Дай структурированный вывод на русском:
       - ⚠️ **Найденные опасные/подозрительные добавки** (сделай акцент на данных из локальной базы)
       - ✅ **Общий вердикт безопасности** (Безопасно / С осторожностью / Опасно)
    
    В КОНЦЕ всегда добавляй этот дисклеймер:
    "⚠️ *Дисклеймер: Бот не является медицинской службой. Информация носит исключительно ознакомительный характер и не учитывает индивидуальные особенности здоровья (аллергии, хронические заболевания). Перед изменением рациона проконсультируйтесь с врачом.*"
    """

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cripto777/EcheckFoodBot",
        "X-Title": "EcheckFoodBot"
    }
    
    payload = {
        "model": "google/gemini-flash-1.5",
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Текст с этикетки: {extracted_text}\n\nОтчет локальной базы:\n{local_db_report}"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                ]
            }
        ],
        "max_tokens": 800
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status == 200:
                result = await response.json()
                return result["choices"][0]["message"]["content"]
            else:
                logging.error(f"Ошибка OpenRouter: {response.status}")
                return "❌ Произошла ошибка при анализе через ИИ. Попробуйте позже."

def get_main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📸 Проверить новый продукт", callback_data="new_check"))
    builder.row(InlineKeyboardButton(text="ℹ️ Инфо и Политика конфиденциальности", callback_data="info"))
    return builder.as_markup()

@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    welcome_text = (
        "👋 Привет! Я **EcheckFoodBot** — твой помощник по безопасности продуктов.\n\n"
        "📸 Отправь мне фотографию этикетки с составом, и я:\n"
        "1️⃣ Мгновенно проверю E-добавки по собственной базе данных.\n"
        "2️⃣ Использую ИИ для глубокого анализа всего состава.\n\n"
        "🔒 *Конфиденциальность:* Мы не собираем и не передаем ваши личные данные третьим лицам, кроме как для обработки запроса через защищенный API ИИ. Вы можете удалить свои данные, просто перестав использовать бота.\n\n"
        "Нажмите кнопку ниже, чтобы начать!"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu_keyboard())

@router.callback_query(F.data == "new_check")
async def start_check(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AnalysisState.waiting_for_photo)
    await callback.message.edit_text("📸 Отправьте фотографию этикетки с составом продукта.\n\n💡 *Совет:* Убедитесь, что текст хорошо освещен и читаем.")
    await callback.answer()

@router.callback_query(F.data == "info")
async def show_info(callback: types.CallbackQuery):
    info_text = (
        "ℹ️ **О боте EcheckFoodBot**\n\n"
        "Бот анализирует состав продуктов питания, выявляя потенциально опасные пищевые добавки (индексы «Е») и их производные.\n\n"
        "⚠️ **Дисклеймер:** Бот не является медицинской службой. Вся информация носит исключительно ознакомительный характер. Бот не учитывает индивидуальные особенности здоровья (аллергии, хронические заболевания), если они не указаны явно. Перед кардинальными изменениями в рационе обязательно проконсультируйтесь с врачом.\n\n"
        "🔒 **Политика конфиденциальности:** Мы не собираем и не храним ваши персональные данные. Изображения обрабатываются в реальном времени и не сохраняются на наших серверах. Данные передаются в API ИИ только для генерации ответа."
    )
    await callback.message.edit_text(info_text, reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.message(AnalysisState.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    status_msg = await message.answer("🔍 Анализирую состав... Сначала проверяю локальную базу, затем подключаю ИИ.")
    
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}") as resp:
            image_bytes = await resp.read()

    image_base64 = base64.b64encode(image_bytes).decode('utf-8')
    
    # ШАГ 1: Распознавание текста (OCR)
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/cripto777/EcheckFoodBot",
        "X-Title": "EcheckFoodBot"
    }
    
    payload_ocr = {
        "model": "google/gemini-flash-1.5",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Извлеки ВЕСЬ видимый текст ингредиентов с этого фото. Верни только чистый текст, без комментариев."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}}
                ]
            }
        ],
        "max_tokens": 500
    }
    
    extracted_text = ""
    async with aiohttp.ClientSession() as session:
        async with session.post("https://openrouter.ai/api/v1/chat/completions", json=payload_ocr, headers=headers) as resp:
            if resp.status == 200:
                res = await resp.json()
                extracted_text = res["choices"][0]["message"]["content"]
            else:
                await status_msg.edit_text("❌ Не удалось распознать текст на фото. Попробуйте сделать фото четче.", reply_markup=get_main_menu_keyboard())
                await state.clear()
                return

    # ШАГ 2: Проверка по локальной базе (0 затрат API)
    found_additives = find_e_additives_in_text(extracted_text)
    
    if found_additives:
        local_report = "⚠️ ВНИМАНИЕ! Обнаружены добавки из базы:\n"
        for item in found_additives:
            local_report += f"• **{item['code']}** ({item['info']['name']}): Опасность - {item['info']['danger']}. {item['info']['description']}\n"
    else:
        local_report = "✅ По локальной базе явных опасных E-добавок не найдено. Передаю полный состав на глубокий анализ ИИ."

    # ШАГ 3: Глубокий анализ через ИИ
    final_analysis = await analyze_with_openrouter(image_bytes, local_report, extracted_text)
    
    response_text = f"📋 **Распознанный текст (фрагмент):**\n_{extracted_text[:300]}..._\n\n{final_analysis}"
    
    await status_msg.edit_text(response_text, reply_markup=get_main_menu_keyboard(), parse_mode="Markdown")
    await state.clear()

@router.message(AnalysisState.waiting_for_photo)
async def handle_wrong_input(message: types.Message):
    await message.answer("Пожалуйста, отправьте именно **фотографию** этикетки с составом.", reply_markup=get_main_menu_keyboard())

@router.message(Command("quantity"))
async def admin_quantity(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🔧 Админ-панель: Статистика пользователей будет здесь.")

async def main():
    print("🤖 Бот EcheckFoodBot запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
