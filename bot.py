import os
import re
import json
import logging
import io
import numpy as np
from PIL import Image, ImageFilter, ImageEnhance

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiohttp
from dotenv import load_dotenv

# Инициализация EasyOCR (загружается один раз при старте, gpu=False для совместимости с хостингом)
import easyocr
logging.getLogger("easyocr").setLevel(logging.WARNING) # Скрываем спам от easyocr
reader = easyocr.Reader(['ru', 'en'], gpu=False)

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

def preprocess_image(image_bytes: bytes) -> bytes:
    """Предобработка изображения для максимального качества OCR"""
    image = Image.open(io.BytesIO(image_bytes)).convert('L') # 1. Черно-белый режим
    
    # 2. Увеличение размера, если разрешение маленькое (модели любят >1000px по ширине)
    if image.width < 1000:
        scale_factor = 1000 / image.width
        new_size = (int(image.width * scale_factor), int(image.height * scale_factor))
        image = image.resize(new_size, Image.Resampling.LANCZOS)
    
    # 3. Увеличение резкости
    image = image.filter(ImageFilter.SHARPEN)
    
    # 4. Повышение контрастности (коэффициент 2.0 делает текст четче на фоне)
    enhancer = ImageEnhance.Contrast(image)
    image = enhancer.enhance(2.0)
    
    # Сохраняем обратно в байты
    img_byte_arr = io.BytesIO()
    image.save(img_byte_arr, format='PNG')
    return img_byte_arr.getvalue()

def extract_text_from_image(image_bytes: bytes) -> str:
    """Распознает текст с помощью EasyOCR"""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        img_array = np.array(image)
        # detail=0 возвращает только список строк текста без координат и уверенности
        result = reader.readtext(img_array, detail=0, paragraph=True) 
        return " ".join(result)
    except Exception as e:
        logging.error(f"Ошибка OCR: {e}")
        return ""

def find_e_additives_in_text(text: str) -> list:
    """Ищет E-добавки в распознанном тексте"""
    matches = re.findall(r'\b[eе]\s?(\d{3,4})\b', text, re.IGNORECASE)
    found = []
    for match in matches:
        e_code = f"E{match.upper()}"
        if e_code in E_DB:
            found.append({"code": e_code, "info": E_DB[e_code]})
        else:
            found.append({"code": e_code, "info": {"name": "Неизвестная добавка", "danger": "Неизвестно", "description": "Отсутствует в локальной базе."}})
    return found

async def analyze_text_with_openrouter(ocr_text: str, local_db_report: str) -> str:
    """Отправляет ТОЛЬКО ТЕКСТ в Gemini для анализа (дешевле и быстрее)"""
    
    system_prompt = """Ты — эксперт по безопасности пищевых продуктов. 
    Тебе передан текст, распознанный с фото этикетки. В нем может быть маркетинговый шум.
    
    ТВОИ ЗАДАЧИ:
    1. Найди в тексте блок, начинающийся со слов "Состав", "Ингредиенты" или подобный. Анализируй ТОЛЬКО его. Игнорируй рекламу, штрих-коды и адреса.
    2. Внимательно изучи "Отчет локальной базы". Если там есть предупреждения, ОБЯЗАТЕЛЬНО выдели их в начале ответа.
    3. Найди другие потенциально опасные компоненты (сахар, трансжиры, аллергены), даже если у них нет индекса "Е".
    4. Дай структурированный вывод на русском языке:
       - ⚠️ **Найденные опасные/подозрительные добавки** (с акцентом на данные из локальной базы)
       - ℹ️ **Другие компоненты, требующие внимания** (если есть)
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
                "content": f"РАСПОЗНАННЫЙ ТЕКСТ С ЭТИКЕТКИ:\n{ocr_text}\n\nОТЧЕТ ЛОКАЛЬНОЙ БАЗЫ:\n{local_db_report}"
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
                error_text = await response.text()
                logging.error(f"Ошибка OpenRouter: {response.status} - {error_text}")
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
        "1️⃣ Улучшу качество фото и извлеку из него текст.\n"
        "2️⃣ Мгновенно проверю E-добавки по собственной базе данных.\n"
        "3️⃣ Использую ИИ для глубокого анализа ТОЛЬКО списка ингредиентов.\n\n"
        "🔒 *Конфиденциальность:* Мы не собираем и не передаем ваши личные данные третьим лицам, кроме как для обработки запроса через защищенный API ИИ. Вы можете удалить свои данные, просто перестав использовать бота.\n\n"
        "Нажмите кнопку ниже, чтобы начать!"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu_keyboard())

@router.callback_query(F.data == "new_check")
async def start_check(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AnalysisState.waiting_for_photo)
    await callback.message.edit_text("📸 Отправьте фотографию этикетки с составом продукта.\n\n💡 *Совет:* Старайтесь, чтобы текст был ровным и хорошо освещенным.")
    await callback.answer()

@router.callback_query(F.data == "info")
async def show_info(callback: types.CallbackQuery):
    info_text = (
        "ℹ️ **О боте EcheckFoodBot**\n\n"
        "Бот анализирует состав продуктов питания, выявляя потенциально опасные пищевые добавки (индексы «Е») и их производные.\n\n"
        "⚠️ **Дисклеймер:** Бот не является медицинской службой. Вся информация носит исключительно ознакомительный характер. Бот не учитывает индивидуальные особенности здоровья (аллергии, хронические заболевания), если они не указаны явно. Перед кардинальными изменениями в рационе обязательно проконсультируйтесь с врачом.\n\n"
        "🔒 **Политика конфиденциальности:** Мы не собираем и не храним ваши персональные данные. Изображения обрабатываются в оперативной памяти в реальном времени и не сохраняются на наших серверах."
    )
    await callback.message.edit_text(info_text, reply_markup=get_main_menu_keyboard())
    await callback.answer()

@router.message(AnalysisState.waiting_for_photo, F.photo)
async def process_photo(message: types.Message, state: FSMContext):
    status_msg = await message.answer("🔍 Получаю фото... Улучшаю качество и распознаю текст (это может занять 5-10 секунд).")
    
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    
    # Скачиваем исходное фото
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file.file_path}") as resp:
            original_image_bytes = await resp.read()

    await status_msg.edit_text("⚙️ Обрабатываю изображение и извлекаю текст...")
    
    # ШАГ 1: Предобработка изображения (Ч/Б, резкость, контраст, ресайз)
    processed_image_bytes = preprocess_image(original_image_bytes)
    
    # ШАГ 2: OCR распознавание текста
    extracted_text = extract_text_from_image(processed_image_bytes)
    
    if not extracted_text or len(extracted_text) < 10:
        await status_msg.edit_text("❌ Не удалось распознать текст на фото. Попробуйте сделать фото четче, ровнее и при лучшем освещении.", reply_markup=get_main_menu_keyboard())
        await state.clear()
        return

    # ШАГ 3: Проверка по локальной базе (мгновенно, 0 затрат API)
    found_additives = find_e_additives_in_text(extracted_text)
    
    if found_additives:
        local_report = "⚠️ ВНИМАНИЕ! Обнаружены добавки из базы:\n"
        for item in found_additives:
            local_report += f"• **{item['code']}** ({item['info']['name']}): Опасность - {item['info']['danger']}. {item['info']['description']}\n"
    else:
        local_report = "✅ По локальной базе явных опасных E-добавок не найдено. Передаю текст на глубокий анализ ИИ."

    # ШАГ 4: Глубокий анализ через ИИ (отправляем ТОЛЬКО текст, без картинки)
    await status_msg.edit_text("🧠 Анализирую состав с помощью ИИ...")
    final_analysis = await analyze_text_with_openrouter(extracted_text, local_report)
    
    response_text = f"📋 **Распознанный текст (фрагмент):**\n_{extracted_text[:250]}..._\n\n{final_analysis}"
    
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
    print("⏳ Загрузка моделей EasyOCR (может занять минуту при первом запуске)...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
