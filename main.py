import asyncio
import logging
import httpx
import os
import math
import gc  # ДОБАВЛЕНО: для полной очистки памяти
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- НАСТРОЙКИ ---
API_TOKEN = '8466170276:AAFlob5hAA1oTFS5rtDpiDpvco-tKW0zPG8'
CF_API_KEY = '$2a$10$.GHpJbr3exg35LxCoFgokeVU.uCzpPex5nQa4YyT7rsvWzO4MS1Aa'

CF_BASE = "https://api.curseforge.com/v1"
MR_BASE = "https://api.modrinth.com/v2"
HEADERS_CF = {"x-api-key": CF_API_KEY}
HEADERS_MR = {"User-Agent": "MinecraftBot/1.0 (contact@example.com)"}
ITEMS_PER_PAGE = 12

# ИЗМЕНЕНО: WARNING вместо INFO, чтобы бот не тратил память на запись каждого шага
logging.basicConfig(level=logging.WARNING, filename="errors.log", 
                    format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# ДОБАВЛЕНО: Лимиты на соединения, чтобы httpx не «раздувался» в памяти
limits = httpx.Limits(max_connections=5, max_keepalive_connections=2)
client = httpx.AsyncClient(timeout=20.0, limits=limits)

class SearchState(StatesGroup):
    category = State()
    query = State()

CATEGORIES = {
    "mc-mod": "Моды",
    "resourcepack": "Ресурспаки",
    "datapack": "Датапаки",
    "shader": "Шейдеры",
    "modpack": "Модпаки",
    "plugin": "Плагины"
}

# ДОБАВЛЕНО: Фоновая задача, которая вычищает мусор из ОЗУ каждые 2 минуты
async def memory_cleaner():
    while True:
        await asyncio.sleep(120)
        gc.collect()

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    await state.clear()
    builder = InlineKeyboardBuilder()
    for code, name in CATEGORIES.items():
        builder.button(text=name, callback_data=f"cat_{code}")
    builder.adjust(2)
    await msg.answer("Привет! Что будем искать сегодня?", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("cat_"))
async def set_category(call: types.CallbackQuery, state: FSMContext):
    category = call.data.split("_")[1]
    await state.update_data(category=category)
    await call.message.edit_text(f"Выбрано: {CATEGORIES[category]}.\nНапиши название для поиска:")
    await state.set_state(SearchState.query)

@dp.message(SearchState.query)
async def process_search(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    cat = data['category']
    query = msg.text
    
    found_mr, found_cf = [], []

    # 1. Поиск Modrinth
    try:
        r_mr = await client.get(f"{MR_BASE}/search", params={"query": query, "facets": f'[["project_type:{cat.replace("mc-mod", "mod")}"]]'}, headers=HEADERS_MR)
        if r_mr.status_code == 200: found_mr = r_mr.json()['hits']
    except Exception as e: logging.error(f"MR Search Error: {e}")

    # 2. Поиск CurseForge
    try:
        class_id = {"mc-mod": 6, "resourcepack": 12, "shader": 6552, "modpack": 4471, "plugin": 5, "datapack": 6945}.get(cat, 6)
        r_cf = await client.get(f"{CF_BASE}/mods/search", params={"gameId": 432, "classId": class_id, "searchFilter": query, "pageSize": 5}, headers=HEADERS_CF)
        if r_cf.status_code == 200: found_cf = r_cf.json()['data']
    except Exception as e: logging.error(f"CF Search Error: {e}")

    if not found_mr and not found_cf:
        return await msg.answer("Ничего не найдено. Попробуй другое название.")

    builder = InlineKeyboardBuilder()
    added_names = set()

    for hit in found_mr[:5]:
        name = hit['title'].lower().strip()
        added_names.add(name)
        builder.button(text=f"[MR] {hit['title']}", callback_data=f"proj_mr_{hit['project_id']}_{cat}")

    for mod in found_cf:
        name = mod['name'].lower().strip()
        if name not in added_names:
            builder.button(text=f"[CF] {mod['name']}", callback_data=f"proj_cf_{mod['id']}_{cat}")

    builder.adjust(1)
    await msg.answer("Найденные результаты:", reply_markup=builder.as_markup())
    gc.collect() # ДОБАВЛЕНО: Чистка после тяжелого поиска

@dp.callback_query(F.data.startswith("proj_"))
async def select_loader(call: types.CallbackQuery):
    _, src, p_id, cat = call.data.split("_")
    
    if cat not in ["mc-mod", "mod"]:
        return await render_versions(call, src, p_id, "any", 0)

    loaders_available = set()
    if src == "mr":
        r_v = await client.get(f"{MR_BASE}/project/{p_id}/version", headers=HEADERS_MR)
        if r_v.status_code == 200:
            for v in r_v.json(): loaders_available.update([l.lower() for l in v['loaders']])
    else:
        loaders_available = {"forge", "fabric", "quilt", "neoforge"}

    builder = InlineKeyboardBuilder()
    for l in ["Forge", "Fabric", "Quilt", "NeoForge"]:
        if l.lower() in loaders_available:
            builder.button(text=l, callback_data=f"vers_{src}_{p_id}_{l.lower()}_0")
    
    builder.adjust(2)
    await call.message.edit_text("Выбери Loader:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("vers_"))
async def handle_pagination(call: types.CallbackQuery):
    params = call.data.split("_")
    src, p_id, loader, page = params[1], params[2], params[3], int(params[4])
    await render_versions(call, src, p_id, loader, page)

async def render_versions(call, src, p_id, loader, page):
    all_versions_map = {} 

    if src == "mr":
        r = await client.get(f"{MR_BASE}/project/{p_id}/version", headers=HEADERS_MR)
        if r.status_code == 200:
            for v in r.json():
                if loader == "any" or loader in [l.lower() for l in v['loaders']]:
                    for g_ver in v['game_versions']:
                        if g_ver not in all_versions_map:
                            all_versions_map[g_ver] = v['id']
    else:
        r = await client.get(f"{CF_BASE}/mods/{p_id}", headers=HEADERS_CF)
        if r.status_code == 200:
            for idx in r.json()['data']['latestFilesIndexes']:
                v_name = idx['gameVersion']
                if v_name not in all_versions_map:
                    all_versions_map[v_name] = idx['fileId']

    sorted_names = sorted(all_versions_map.keys(), 
                          key=lambda x: [int(d) for d in x.split('.') if d.isdigit()], 
                          reverse=True)

    total_pages = math.ceil(len(sorted_names) / ITEMS_PER_PAGE)
    start, end = page * ITEMS_PER_PAGE, (page + 1) * ITEMS_PER_PAGE
    current_page = sorted_names[start:end]

    builder = InlineKeyboardBuilder()
    for v_name in current_page:
        builder.button(text=v_name, callback_data=f"dl_{src}_{p_id}_{all_versions_map[v_name]}")
    
    builder.adjust(3)
    
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton(text="⬅️ Назад", callback_data=f"vers_{src}_{p_id}_{loader}_{page-1}"))
    if end < len(sorted_names):
        nav.append(types.InlineKeyboardButton(text="Вперед ➡️", callback_data=f"vers_{src}_{p_id}_{loader}_{page+1}"))
    
    if nav: builder.row(*nav)
    builder.row(types.InlineKeyboardButton(text="🏠 В начало", callback_data="start_over"))

    await call.message.edit_text(f"Версии для {loader.upper()} (Стр. {page+1}/{max(1, total_pages)}):", 
                                reply_markup=builder.as_markup())
    # ДОБАВЛЕНО: Сразу очищаем временные данные
    all_versions_map.clear()
    gc.collect()

@dp.callback_query(F.data == "start_over")
async def start_over(call: types.CallbackQuery, state: FSMContext):
    await cmd_start(call.message, state)

@dp.callback_query(F.data.startswith("dl_"))
async def download_file(call: types.CallbackQuery):
    _, src, p_id, f_id = call.data.split("_")
    await call.message.edit_text("⏳ Подготовка файла...")

    if src == "mr":
        try:
            r = await client.get(f"{MR_BASE}/version/{f_id}", headers=HEADERS_MR)
            file_info = r.json()['files'][0]
            f_data = await client.get(file_info['url'])
            
            file_path = file_info['filename']
            with open(file_path, "wb") as f: f.write(f_data.content)
            
            await call.message.answer_document(types.FSInputFile(file_path), caption=f"✅ Файл: {file_path}")
            os.remove(file_path)
            await call.message.delete()
        except Exception as e:
            await call.message.answer(f"Ошибка при скачивании: {e}")
    else:
        # ИСПРАВЛЕННАЯ ЛОГИКА ДЛЯ CURSEFORGE
        try:
            # Сначала получаем информацию о проекте, чтобы достать его slug и тип контента
            r_project = await client.get(f"{CF_BASE}/mods/{p_id}", headers=HEADERS_CF)
            if r_project.status_code == 200:
                proj_data = r_project.json()['data']
                slug = proj_data['slug']
                
                # Определяем правильный раздел в URL на основе classId
                # 6 - mods, 12 - resource-packs, 6552 - shaders и т.д.
                cid = proj_data['classId']
                section = {6: "mc-mods", 12: "texture-packs", 6552: "customization", 4471: "modpacks", 5: "bukkit-plugins", 6945: "data-packs"}.get(cid, "mc-mods")
                
                # Формируем рабочую ссылку на страницу загрузки
                download_url = f"https://www.curseforge.com/minecraft/{section}/{slug}/download/{f_id}"
                await call.message.answer(f"✅ Ссылка на скачивание:\n{download_url}")
            else:
                await call.message.answer("❌ Не удалось получить данные о проекте.")
        except Exception as e:
            logging.error(f"CF Link Error: {e}")
            await call.message.answer("❌ Ошибка при формировании ссылки.")

    gc.collect() # ДОБАВЛЕНО: Чистка после скачивания

async def main():
    # ДОБАВЛЕНО: Запуск «уборщика» памяти в фоне
    asyncio.create_task(memory_cleaner())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())