from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from unittest.mock import AsyncMock, MagicMock

import bot
import config
import database


class TestContestBot(unittest.IsolatedAsyncioTestCase):
    """Comprehensive unit and integration tests for the Telegram Contest Bot."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_contest.db")
        database.init_db(self.db_path)
        self._orig_admin_id = config.ADMIN_ID
        self._orig_admin_username = getattr(config, "ADMIN_USERNAME", None)
        config.ADMIN_ID = None
        config.ADMIN_USERNAME = "wwtivan"

    def tearDown(self) -> None:
        config.ADMIN_ID = self._orig_admin_id
        config.ADMIN_USERNAME = self._orig_admin_username
        self.temp_dir.cleanup()

    # -----------------------------------------------------------------------
    # 1. Sequential Numbering (1, 2, 3...)
    # -----------------------------------------------------------------------
    def test_sequential_numbering(self) -> None:
        r1 = bot.handle_participation(1001, "Иван", "Иванов", "ivan_1", db_path=self.db_path)
        r2 = bot.handle_participation(1002, "Мария", "Петрова", "maria_2", db_path=self.db_path)
        r3 = bot.handle_participation(1003, "Алексей", None, None, db_path=self.db_path)

        self.assertTrue(r1["active"])
        self.assertTrue(r1["is_new"])
        self.assertEqual(r1["number"], 1)
        self.assertIn("<b>№1</b>", r1["user_message"])

        self.assertTrue(r2["is_new"])
        self.assertEqual(r2["number"], 2)
        self.assertIn("<b>№2</b>", r2["user_message"])

        self.assertTrue(r3["is_new"])
        self.assertEqual(r3["number"], 3)
        self.assertIn("<b>№3</b>", r3["user_message"])

        all_p = database.get_all_participants(db_path=self.db_path)
        self.assertEqual([p["number"] for p in all_p], [1, 2, 3])
        self.assertEqual([p["user_id"] for p in all_p], [1001, 1002, 1003])

    # -----------------------------------------------------------------------
    # 2. Duplicate Registration Protection
    # -----------------------------------------------------------------------
    def test_duplicate_registration_protection(self) -> None:
        first = bot.handle_participation(2001, "Дмитрий", "Смирнов", "dmitry", db_path=self.db_path)
        self.assertTrue(first["is_new"])
        self.assertEqual(first["number"], 1)

        # Register another user to advance sequence to 2
        second_user = bot.handle_participation(2002, "Ольга", None, "olga", db_path=self.db_path)
        self.assertTrue(second_user["is_new"])
        self.assertEqual(second_user["number"], 2)

        # Same user 2001 clicks "Участвовать" multiple times
        dup1 = bot.handle_participation(2001, "Дмитрий", "Смирнов", "dmitry", db_path=self.db_path)
        dup2 = bot.handle_participation(2001, "Дмитрий", "Смирнов", "dmitry_new", db_path=self.db_path)

        self.assertFalse(dup1["is_new"])
        self.assertEqual(dup1["number"], 1)
        self.assertIn("Вы уже участвуете", dup1["user_message"])
        self.assertIn("<b>№1</b>", dup1["user_message"])
        self.assertIsNone(dup1["admin_notification"])

        self.assertFalse(dup2["is_new"])
        self.assertEqual(dup2["number"], 1)
        self.assertEqual(database.get_participants_count(db_path=self.db_path), 2)

        # Verify updated username was saved without changing participant number
        p = database.get_participant(2001, db_path=self.db_path)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["number"], 1)
        self.assertEqual(p["username"], "dmitry_new")

    # -----------------------------------------------------------------------
    # 3. Hidden Nickname Formatting & HTML Escaping
    # -----------------------------------------------------------------------
    def test_hidden_nickname_formatting_and_html_escaping(self) -> None:
        # User with username "alex_1"
        line_with_uname = database.format_participant_line(
            {
                "number": 1,
                "first_name": "Александр",
                "last_name": "Коваленко",
                "username": "alex_1",
            }
        )
        self.assertEqual(
            line_with_uname,
            "1. Александр Коваленко <tg-spoiler>@alex_1</tg-spoiler>",
        )

        # User without username (None)
        line_without_uname_none = database.format_participant_line(
            {
                "number": 2,
                "first_name": "Елена",
                "last_name": None,
                "username": None,
            }
        )
        self.assertEqual(
            line_without_uname_none,
            "2. Елена <tg-spoiler>@username</tg-spoiler>",
        )

        # User without username (empty string "" or "@")
        line_without_uname_empty = database.format_participant_line(
            {
                "number": 3,
                "first_name": "Максим",
                "last_name": "",
                "username": "   ",
            }
        )
        self.assertEqual(
            line_without_uname_empty,
            "3. Максим <tg-spoiler>@username</tg-spoiler>",
        )

        # User with special HTML characters in name: First <script> & Co
        line_special_html = database.format_participant_line(
            {
                "number": 4,
                "first_name": "First <script>",
                "last_name": "& Co >",
                "username": "user_html",
            }
        )
        self.assertEqual(
            line_special_html,
            "4. First &lt;script&gt; &amp; Co &gt; <tg-spoiler>@user_html</tg-spoiler>",
        )
        self.assertNotIn("<script>", line_special_html)
        self.assertIn("<tg-spoiler>@user_html</tg-spoiler>", line_special_html)

        # Admin notification format check
        notif = bot.format_new_participant_admin_notification(
            number=5,
            first_name="First <script> & Co",
            last_name=None,
            username=None,
        )
        self.assertIn("First &lt;script&gt; &amp; Co", notif)
        self.assertIn("<tg-spoiler>@username</tg-spoiler>", notif)

    # -----------------------------------------------------------------------
    # 4. Finishing Contest, Full Numbered List 1..N, and Rejecting New Users
    # -----------------------------------------------------------------------
    def test_finish_contest_and_reject_new_participants(self) -> None:
        bot.handle_participation(3001, "Анна", None, "anna_tg", db_path=self.db_path)
        bot.handle_participation(3002, "Борис", "Б.", None, db_path=self.db_path)
        bot.handle_participation(3003, "Виктор", None, "viktor_3", db_path=self.db_path)

        self.assertTrue(database.is_contest_active(db_path=self.db_path))

        messages = bot.finish_contest_and_get_messages(db_path=self.db_path)
        self.assertFalse(database.is_contest_active(db_path=self.db_path))
        self.assertEqual(len(messages), 1)
        full_text = messages[0]

        self.assertIn("Конкурс завершён! Список всех участников (3):", full_text)
        self.assertIn("1. Анна <tg-spoiler>@anna_tg</tg-spoiler>", full_text)
        self.assertIn("2. Борис Б. <tg-spoiler>@username</tg-spoiler>", full_text)
        self.assertIn("3. Виктор <tg-spoiler>@viktor_3</tg-spoiler>", full_text)

        # Reject new participants after contest ends
        late = bot.handle_participation(3004, "Опоздавший", None, "late_user", db_path=self.db_path)
        self.assertFalse(late["active"])
        self.assertFalse(late["is_new"])
        self.assertIsNone(late["number"])
        self.assertEqual(
            late["user_message"],
            "⛔ Конкурс уже завершён! Ожидайте начала нового конкурса.",
        )
        self.assertEqual(database.get_participants_count(db_path=self.db_path), 3)

    # -----------------------------------------------------------------------
    # 5. Message Chunking / Pagination for 150+ Participants (< 4096 chars)
    # -----------------------------------------------------------------------
    def test_message_chunking_for_150_participants(self) -> None:
        for i in range(1, 151):
            uname = f"participant_user_{i}" if i % 2 == 1 else None
            database.register_participant(
                user_id=5000 + i,
                first_name=f"Участник_{i}",
                last_name="Конкурса_ДлиннаяФамилия",
                username=uname,
                db_path=self.db_path,
            )

        messages = bot.finish_contest_and_get_messages(db_path=self.db_path)
        self.assertGreater(len(messages), 1, "150 participants should split into multiple chunks")

        for chunk in messages:
            self.assertLessEqual(len(chunk), 4096, "No message chunk may exceed Telegram's 4096 limit")
            self.assertLessEqual(len(chunk), bot.MAX_MESSAGE_LENGTH)

        combined = "\n".join(messages)
        self.assertIn("1. Участник_1 Конкурса_ДлиннаяФамилия <tg-spoiler>@participant_user_1</tg-spoiler>", combined)
        self.assertIn("2. Участник_2 Конкурса_ДлиннаяФамилия <tg-spoiler>@username</tg-spoiler>", combined)
        self.assertIn("150. Участник_150 Конкурса_ДлиннаяФамилия <tg-spoiler>@username</tg-spoiler>", combined)

    # -----------------------------------------------------------------------
    # 6. Resetting / Starting a New Contest
    # -----------------------------------------------------------------------
    def test_reset_contest_restarts_numbering_at_1(self) -> None:
        bot.handle_participation(4001, "Первый", None, "first", db_path=self.db_path)
        bot.handle_participation(4002, "Второй", None, "second", db_path=self.db_path)
        bot.finish_contest_and_get_messages(db_path=self.db_path)
        self.assertFalse(database.is_contest_active(db_path=self.db_path))
        self.assertEqual(database.get_participants_count(db_path=self.db_path), 2)

        # Reset contest
        database.reset_contest(db_path=self.db_path)
        self.assertTrue(database.is_contest_active(db_path=self.db_path))
        self.assertEqual(database.get_participants_count(db_path=self.db_path), 0)

        # First participant in the new contest must get number 1
        new_first = bot.handle_participation(4009, "НовыйПервый", None, "new_1", db_path=self.db_path)
        self.assertTrue(new_first["active"])
        self.assertTrue(new_first["is_new"])
        self.assertEqual(new_first["number"], 1)

    # -----------------------------------------------------------------------
    # 7. Reserved / Booked Spots (Skipping 1 & 55 -> 56, Parsing, Admin-Only)
    # -----------------------------------------------------------------------
    async def test_reserved_spots_skipping_55_to_56_and_admin_only(self) -> None:
        # Verify parse_reservation_line formats
        self.assertEqual(
            bot.parse_reservation_line("55 Иван Иванов @ivan"),
            (55, "Иван Иванов", "ivan"),
        )
        self.assertEqual(
            bot.parse_reservation_line("№55 Иван Иванов"),
            (55, "Иван Иванов", None),
        )
        self.assertEqual(
            bot.parse_reservation_line("55 @ivan"),
            (55, "Участник", "ivan"),
        )
        self.assertIsNone(bot.parse_reservation_line("обычный текст без номера"))

        # Regular admin keyboard must NEVER show "Бронь мест" (neither while active nor after finishing)
        active_kb_texts = [
            btn.text for row in bot.get_admin_inline_keyboard(db_path=self.db_path).inline_keyboard for btn in row
        ]
        self.assertFalse(any("Бронь мест" in t for t in active_kb_texts))
        database.set_contest_active(False, db_path=self.db_path)
        finished_kb_texts = [
            btn.text for row in bot.get_admin_inline_keyboard(db_path=self.db_path).inline_keyboard for btn in row
        ]
        self.assertFalse(any("Бронь мест" in t for t in finished_kb_texts))

        # Secret /admin2 opens reservation menu strictly for admin
        config.ADMIN_ID = None
        config.ADMIN_USERNAME = "wwtivan"
        admin_msg = MagicMock()
        admin_msg.from_user = MagicMock(id=7001, first_name="Ivan", last_name=None, username="wwtivan")
        admin_msg.answer = AsyncMock()
        await bot.cmd_admin2(admin_msg, db_path=self.db_path)
        admin_msg.answer.assert_awaited_once()
        self.assertIn("Бронирование мест", admin_msg.answer.call_args.args[0])

        stranger_admin2_msg = MagicMock()
        stranger_admin2_msg.from_user = MagicMock(id=7777, first_name="Stranger", last_name=None, username="stranger")
        stranger_admin2_msg.answer = AsyncMock()
        await bot.cmd_admin2(stranger_admin2_msg, db_path=self.db_path)
        stranger_admin2_msg.answer.assert_not_awaited()

        # Sub-case A: Reserve spot 1 before contest starts -> reset_contest() starts contest -> first real user gets 2
        ok1, _ = bot.handle_reserve_command("1 Первый Бронь @first_vip", db_path=self.db_path)
        self.assertTrue(ok1)
        database.reset_contest(db_path=self.db_path)
        # Pre-reserved spots survive reset_contest() so admin can reserve before starting a new contest
        self.assertEqual(len(database.get_reserved_spots(db_path=self.db_path)), 1)
        first_real = bot.handle_participation(6000, "Реальный", "ПослеПервого", "real_2", db_path=self.db_path)
        self.assertEqual(first_real["number"], 2)
        database.clear_reserved_spots(db_path=self.db_path)

        # Sub-case B: Reserve spot 55 ("55 Иван Иванов @ivan"), start contest,
        # register 54 real users (1..54), next real user must skip 55 and get 56!
        database.set_contest_active(False, db_path=self.db_path)
        ok55, _ = bot.handle_reserve_command("55 Иван Иванов @ivan", db_path=self.db_path)
        self.assertTrue(ok55)
        database.reset_contest(db_path=self.db_path)

        for i in range(1, 55):
            r = bot.handle_participation(
                user_id=6000 + i,
                first_name=f"Участник_{i}",
                last_name=None,
                username=f"user_{i}",
                db_path=self.db_path,
            )
            self.assertEqual(r["number"], i)

        # 55th real participant clicks "Участвовать" -> skips reserved 55 and gets 56!
        r_after_55 = bot.handle_participation(
            user_id=6055,
            first_name="УчастникПосле55",
            last_name=None,
            username="user_56",
            db_path=self.db_path,
        )
        self.assertTrue(r_after_55["is_new"])
        self.assertEqual(r_after_55["number"], 56)

        # Trying to reserve spot #10 (already taken by a real participant) must be rejected
        ok_taken, msg_taken = database.reserve_spot(10, "Занято", "taken", db_path=self.db_path)
        self.assertFalse(ok_taken)
        self.assertIn("уже занято реальным участником", msg_taken)

        # Verify final list has 54, 55 (reserved), and 56 in exact sequential order
        messages = bot.finish_contest_and_get_messages(db_path=self.db_path)
        final_text = "\n".join(messages)
        idx_54 = final_text.find("54. Участник_54 <tg-spoiler>@user_54</tg-spoiler>")
        idx_55 = final_text.find("55. Иван Иванов <tg-spoiler>@ivan</tg-spoiler>")
        idx_56 = final_text.find("56. УчастникПосле55 <tg-spoiler>@user_56</tg-spoiler>")
        self.assertNotEqual(idx_54, -1)
        self.assertNotEqual(idx_55, -1)
        self.assertNotEqual(idx_56, -1)
        self.assertLess(idx_54, idx_55)
        self.assertLess(idx_55, idx_56)

        # Non-admin trying /reserve or direct message "77 Хакер @hacker" is silently ignored
        stranger_msg = MagicMock()
        stranger_msg.from_user = MagicMock(id=7777, first_name="Stranger", last_name=None, username="stranger")
        stranger_msg.text = "77 Хакер @hacker"
        stranger_msg.answer = AsyncMock()

        await bot.cmd_reserve(stranger_msg, command=None, db_path=self.db_path)
        await bot.on_admin_direct_reservation_text(stranger_msg, db_path=self.db_path)
        stranger_msg.answer.assert_not_awaited()
        self.assertEqual([r["number"] for r in database.get_reserved_spots(db_path=self.db_path)], [55])

        # Unreserving spot #55 works
        ok_unres, _ = bot.handle_unreserve_command("/unreserve 55", db_path=self.db_path)
        self.assertTrue(ok_unres)
        self.assertEqual(database.get_reserved_spots(db_path=self.db_path), [])

        # Resetting contest clears real participants while preserving pre-reserved spots
        bot.handle_reserve_command("99 Тест Сброса @reset_test", db_path=self.db_path)
        self.assertEqual(len(database.get_reserved_spots(db_path=self.db_path)), 1)
        database.reset_contest(db_path=self.db_path)
        self.assertEqual([r["number"] for r in database.get_reserved_spots(db_path=self.db_path)], [99])
        self.assertEqual(database.get_participants_count(db_path=self.db_path, include_reserved=False), 0)
        database.clear_reserved_spots(db_path=self.db_path)
        self.assertEqual(database.get_reserved_spots(db_path=self.db_path), [])

    # -----------------------------------------------------------------------
    # 8. Strict Single-Admin (ADMIN_USERNAME=wwtivan / ADMIN_ID) & Silent Ignore
    # -----------------------------------------------------------------------
    async def test_strict_single_admin_username_binding_and_silent_ignore(self) -> None:
        config.ADMIN_ID = None
        config.ADMIN_USERNAME = "wwtivan"

        self.assertEqual(database.get_all_admins(db_path=self.db_path), [])
        self.assertFalse(database.is_admin(7001, username="stranger", db_path=self.db_path))

        # Non-admin calling /admin -> completely ignored (no reply)
        stranger_msg = MagicMock()
        stranger_msg.from_user = MagicMock(id=7001, first_name="Stranger", last_name=None, username="stranger")
        stranger_msg.text = "/admin 1234"
        stranger_msg.answer = AsyncMock()

        await bot.cmd_admin(stranger_msg, command=None, db_path=self.db_path)
        await bot.on_admin_panel_text(stranger_msg, db_path=self.db_path)
        stranger_msg.answer.assert_not_awaited()
        self.assertFalse(database.is_admin(7001, username="stranger", db_path=self.db_path))

        # Owner @wwtivan calling /admin -> automatically bound by username and shown Admin Panel!
        owner_msg = MagicMock()
        owner_msg.from_user = MagicMock(id=888999, first_name="Иван", last_name=None, username="WwtIvan")
        owner_msg.text = "/admin"
        owner_msg.answer = AsyncMock()

        await bot.cmd_admin(owner_msg, command=None, db_path=self.db_path)
        owner_msg.answer.assert_awaited_once()
        self.assertIn("Панель управления конкурсом", owner_msg.answer.call_args[0][0])

        # Verify owner's user_id 888999 is now permanently bound in admin_binding
        self.assertTrue(database.is_admin(888999, username=None, db_path=self.db_path))
        self.assertEqual(database.get_all_admins(db_path=self.db_path), [888999])

        # Even if someone else later spoofs or tries after binding, they are rejected
        self.assertFalse(database.is_admin(7002, username="wwtivan", db_path=self.db_path))

        # Non-admin 7001 clicking admin callbacks gets empty callback.answer() and cannot finish contest
        stranger_cb = MagicMock()
        stranger_cb.from_user = stranger_msg.from_user
        stranger_cb.message = MagicMock()
        stranger_cb.message.answer = AsyncMock()
        stranger_cb.answer = AsyncMock()

        await bot.on_admin_finish_callback(stranger_cb, db_path=self.db_path)
        stranger_cb.message.answer.assert_not_awaited()
        stranger_cb.answer.assert_awaited_once_with()
        self.assertTrue(database.is_contest_active(db_path=self.db_path))

    # -----------------------------------------------------------------------
    # 9. Aiogram Async Handlers Integration Test
    # -----------------------------------------------------------------------
    async def test_aiogram_handlers_flow(self) -> None:
        # Owner @wwtivan (id=999) opens /start first, which auto-binds their ID 999
        owner_start_msg = MagicMock()
        owner_start_msg.from_user = MagicMock(id=999, first_name="Иван", last_name=None, username="wwtivan")
        owner_start_msg.answer = AsyncMock()
        await bot.cmd_start(owner_start_msg, command=None, db_path=self.db_path)
        self.assertEqual(database.get_all_admins(db_path=self.db_path), [999])

        # Participant sends /start
        user_msg = MagicMock()
        user_msg.from_user = MagicMock(id=111, first_name="Сергей", last_name="Тест", username="sergey_t")
        user_msg.answer = AsyncMock()
        user_msg.bot = MagicMock()
        user_msg.bot.send_message = AsyncMock()

        await bot.cmd_start(user_msg, command=None, db_path=self.db_path)
        self.assertEqual(user_msg.answer.await_count, 2)

        # Participant clicks inline "🎉 Участвовать"
        cb = MagicMock()
        cb.from_user = user_msg.from_user
        cb.message = MagicMock()
        cb.message.answer = AsyncMock()
        cb.answer = AsyncMock()
        cb.bot = MagicMock()
        cb.bot.send_message = AsyncMock()

        await bot.on_participate_callback(cb, db_path=self.db_path)
        cb.message.answer.assert_awaited_once()
        sent_user_text = cb.message.answer.call_args[0][0]
        self.assertIn("<b>№1</b>", sent_user_text)

        # Single Admin @wwtivan (999) was notified with hidden spoiler username
        cb.bot.send_message.assert_awaited_once()
        admin_call_kwargs = cb.bot.send_message.call_args.kwargs
        self.assertEqual(admin_call_kwargs["chat_id"], 999)
        self.assertIn("<tg-spoiler>@sergey_t</tg-spoiler>", admin_call_kwargs["text"])

        # Admin @wwtivan (999) clicks "🏁 Закончить конкурс"
        admin_cb = MagicMock()
        admin_cb.from_user = MagicMock(id=999, first_name="Иван", last_name=None, username="wwtivan")
        admin_cb.message = MagicMock()
        admin_cb.message.answer = AsyncMock()
        admin_cb.answer = AsyncMock()

        await bot.on_admin_finish_callback(admin_cb, db_path=self.db_path)
        self.assertFalse(database.is_contest_active(db_path=self.db_path))
        finish_reply = admin_cb.message.answer.call_args[0][0]
        self.assertIn("1. Сергей Тест <tg-spoiler>@sergey_t</tg-spoiler>", finish_reply)

    # -----------------------------------------------------------------------
    # 10. Verify BOT_TOKEN & ADMIN_USERNAME against Config and Telegram getMe
    # -----------------------------------------------------------------------
    def test_bot_token_configured_and_valid_on_telegram(self) -> None:
        self.assertEqual(
            config.BOT_TOKEN,
            "9100137108:AAFlgDK5427hb9SVx1E_UwR2oAfW-E04-Xk",
        )
        self.assertEqual(self._orig_admin_username, "wwtivan")
        url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/getMe"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data.get("ok"), f"Telegram getMe failed: {data}")
        self.assertTrue(data["result"]["is_bot"])
        self.assertEqual(data["result"]["username"], "volkovconcurs_bot")


if __name__ == "__main__":
    unittest.main()
