# plugin/game_activity.py
import os
import json
import threading
import time
from datetime import datetime, timezone

class ClientAPI:
    """
    Абстрактный адаптер для работы с UI клиента.
    Реализуйте методы для конкретного клиента (Exteragram/AyuGram).
    Методы:
      - show_block(icon_path, title, status_text, timer_text)
      - update_block(timer_text)
      - hide_block()
    """
    def show_block(self, icon_path: str, title: str, status_text: str, timer_text: str):
        raise NotImplementedError

    def update_block(self, timer_text: str):
        raise NotImplementedError

    def hide_block(self):
        raise NotImplementedError


class ConsoleAdapter(ClientAPI):
    """
    Простой adapter для отладки в консоли — рисует блок в stdout.
    """
    def __init__(self):
        self.visible = False

    def show_block(self, icon_path: str, title: str, status_text: str, timer_text: str):
        self.visible = True
        print("=== GAME ACTIVITY BLOCK SHOW ===")
        print(f"Icon: {icon_path}")
        print(f"{title} — {status_text}")
        print(f"Timer: {timer_text}")
        print("=================================")

    def update_block(self, timer_text: str):
        if self.visible:
            print(f"[update] Timer: {timer_text}")

    def hide_block(self):
        if self.visible:
            print("=== GAME ACTIVITY BLOCK HIDE ===")
            self.visible = False


class GameActivityManager:
    """
    Менеджер локального бл��ка игровой активности.

    Используйте:
      manager = GameActivityManager(client_api, storage_path=None)
      manager.set_game("AmongUs")
      manager.change_game("CSGO")
      manager.stop_game()
    """
    def __init__(self, client_api: ClientAPI, storage_path: str = None, icons_dir: str = None):
        self.client_api = client_api
        # По умолчанию JSON хранится рядом с модулем
        if storage_path is None:
            storage_path = os.path.join(os.path.dirname(__file__), "game_activity_state.json")
        self.storage_path = storage_path

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        if icons_dir is None:
            icons_dir = os.path.join(base_dir, "icons")
        self.icons_dir = icons_dir

        self._timer_thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.state = {
            "active": False,
            "name": None,
            "start_ts": None  # timestamp UTC
        }
        self._load_state()

        # Если при старте был выбранна игра, возобновим блок/таймер
        if self.state.get("active") and self.state.get("name") and self.state.get("start_ts"):
            self._show_block_resume()

    # --------------------------
    # Внешние API
    # --------------------------
    def set_game(self, name: str):
        """
        Выбрать игру name и запустить таймер (сброс времени на 0).
        """
        with self._lock:
            self.state["name"] = name
            self.state["start_ts"] = datetime.now(timezone.utc).timestamp()
            self.state["active"] = True
            self._save_state()
            icon = self._find_icon_for_game(name)
            title = name
            status_text = "Playing"
            timer_text = self._format_elapsed(0)
            # Покажем блок (клиент-адаптер реализует UI)
            self.client_api.show_block(icon, title, status_text, timer_text)
            # Стартуем цикл обновления
            self._start_timer_thread()

    def stop_game(self):
        """
        Остановить таймер и скрыть блок.
        """
        with self._lock:
            self.state["active"] = False
            self.state["name"] = None
            self.state["start_ts"] = None
            self._save_state()
            self._stop_timer_thread()
            self.client_api.hide_block()

    def change_game(self, name: str):
        """
        Сменить игру: сбрасываем таймер и показываем новую иконку.
        """
        # просто set_game, чтобы было единообразно
        self.set_game(name)

    # --------------------------
    # Внутренние методы
    # --------------------------
    def _find_icon_for_game(self, name: str) -> str:
        """
        Ищет иконку в icons_dir.
        Поддерживаются расширения: png, jpg, jpeg, webp, svg.
        Если не найдено — возвращает пустую строку.
        """
        if not os.path.isdir(self.icons_dir):
            return ""
        candidates = []
        for ext in ("png", "jpg", "jpeg", "webp", "svg"):
            filename = f"{name}.{ext}"
            path = os.path.join(self.icons_dir, filename)
            if os.path.isfile(path):
                candidates.append(path)
        if candidates:
            return candidates[0]
        # fallback: lowercase, replace spaces
        safe_name = name.lower().replace(" ", "_")
        for ext in ("png", "jpg"):
            path = os.path.join(self.icons_dir, f"{safe_name}.{ext}")
            if os.path.isfile(path):
                return path
        return ""

    def _format_elapsed(self, seconds: int) -> str:
        hrs, rem = divmod(int(seconds), 3600)
        mins, secs = divmod(rem, 60)
        if hrs:
            return f"{hrs:d}:{mins:02d}:{secs:02d}"
        else:
            return f"{mins:d}:{secs:02d}"

    def _start_timer_thread(self):
        self._stop_event.clear()
        if self._timer_thread and self._timer_thread.is_alive():
            return
        self._timer_thread = threading.Thread(target=self._run_timer_loop, daemon=True)
        self._timer_thread.start()

    def _stop_timer_thread(self):
        self._stop_event.set()
        if self._timer_thread:
            self._timer_thread.join(timeout=1)
        self._timer_thread = None
        self._stop_event.clear()

    def _run_timer_loop(self):
        # Вызывается в отдельном потоке
        while not self._stop_event.is_set():
            with self._lock:
                if not self.state.get("active") or not self.state.get("start_ts"):
                    break
                elapsed = time.time() - float(self.state["start_ts"])
                timer_text = self._format_elapsed(elapsed)
            try:
                self.client_api.update_block(timer_text)
            except Exception:
                # Адаптер мог падать — игнорируем, чтобы не завершить поток
                pass
            # Sleep по секунде, но прерываемся мгновенно, если нужно
            for _ in range(10):
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)

    def _show_block_resume(self):
        """
        При старте плагина — если state показывает активную игру, 
        показать блок и возобновить таймер.
        """
        name = self.state.get("name")
        start_ts = self.state.get("start_ts")
        if not name or not start_ts:
            return
        icon = self._find_icon_for_game(name)
        title = name
        status_text = "Playing"
        elapsed = time.time() - float(start_ts)
        timer_text = self._format_elapsed(elapsed)
        self.client_api.show_block(icon, title, status_text, timer_text)
        self._start_timer_thread()

    # --------------------------
    # Сохранение/загрузка state
    # --------------------------
    def _save_state(self):
        try:
            tmp = self.storage_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.storage_path)
        except Exception:
            # не критично — просто логировать/игнорировать
            pass

    def _load_state(self):
        try:
            if os.path.isfile(self.storage_path):
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
        except Exception:
            # повреждённый файл — игнорируем
            self.state = {
                "active": False,
                "name": None,
                "start_ts": None
            }


# --------------------------
# Шаблон адаптера для мод-клиентов (пример: Exteragram/AyuGram)
# --------------------------
class TelegramClientAdapter(ClientAPI):
    """
    Шаблон адаптера, адаптируйте под API вашего клиента (Exteragram / AyuGram).
    Псевдокод / подсказки где вставить реальные вызовы:
      - В Exteragram/AyuGram может быть API для добавления локальных блоков на профиль.
      - Обычно нужно зарегистрировать блок по ID, затем обновлять его содержимое.
    Пример (псевдо):
        def show_block(...):
            client.ui.create_local_profile_block(id="game_activity", icon=icon_path, title=title, text=status_text, right_text=timer_text)
    Пожалуйста, замените body методов реальными вызовами клиента.
    """
    def __init__(self, client):
        """
        client: объект клиента (зависит от API Exteragram/AyuGram)
        """
        self.client = client
        self.block_id = "local_game_activity_block"

    def show_block(self, icon_path: str, title: str, status_text: str, timer_text: str):
        # TODO: заменить на реальную интеграцию. Пример интерфейса:
        # self.client.ui.create_local_profile_block(id=self.block_id, icon=icon_path, title=title, subtitle=status_text, right_text=timer_text)
        # Ниже — заглушка:
        try:
            # пример: если клиент использует метод set_local_profile_card
            if hasattr(self.client, "set_local_profile_card"):
                # Псевдокод — конкретная структура зависит от клиента
                self.client.set_local_profile_card({
                    "id": self.block_id,
                    "icon": icon_path,
                    "title": title,
                    "text": status_text,
                    "right_text": timer_text
                })
            else:
                # fallback print
                print("show_block:", icon_path, title, status_text, timer_text)
        except Exception:
            pass

    def update_block(self, timer_text: str):
        # TODO: реализовать обновление содержимого блока
        try:
            if hasattr(self.client, "update_local_profile_card"):
                self.client.update_local_profile_card(self.block_id, {"right_text": timer_text})
            else:
                print("update_block:", timer_text)
        except Exception:
            pass

    def hide_block(self):
        try:
            if hasattr(self.client, "remove_local_profile_card"):
                self.client.remove_local_profile_card(self.block_id)
            else:
                print("hide_block")
        except Exception:
            pass
