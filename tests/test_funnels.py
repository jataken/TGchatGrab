"""С10: настраиваемые воронки — funnel/funnel_stage вместо констант в
core/lead.py, миграция 013 сидирует «Телеграм · биржа» и переносит на
неё существующие заявки без единой правки цвета/подписи, второй воронке
можно завести свои этапы и коды (в т.ч. совпадающие с первой воронкой —
код уникален только в пределах своей воронки), перенос заявки между
воронками пишет отдельное событие и не трогает origin_channel, отчёты
по источникам/направлениям считают won/lost по kind этапа, а не по
жёстко зашитому имени статуса.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _bootstrap import fresh_db
from chatgrab.core import lead as lead_domain

paths, db = fresh_db("cgfunnels")


print("== миграция 013 сидирует «Телеграм · биржа» с теми же кодами/подписями/цветами ==")
funnels = db.list_funnels()
# П9's migration 020 seeds a second funnel ("Почта · прямой запрос") in
# this same fresh_db() run — 013's own seed is picked out by channel
# rather than assumed to be the only row, so this test still only
# verifies what 013 itself produced.
assert len(funnels) == 2, funnels
default_funnel = next(f for f in funnels if f["channel"] == lead_domain.DEFAULT_FUNNEL_CHANNEL)
assert default_funnel["name"] == lead_domain.DEFAULT_FUNNEL_NAME
assert default_funnel["channel"] == lead_domain.DEFAULT_FUNNEL_CHANNEL
stages = db.list_funnel_stages(default_funnel["id"])
assert [s["code"] for s in stages] == [s["code"] for s in lead_domain.DEFAULT_FUNNEL_STAGES]
assert [s["label"] for s in stages] == [s["label"] for s in lead_domain.DEFAULT_FUNNEL_STAGES]
assert [s["kind"] for s in stages] == [s["kind"] for s in lead_domain.DEFAULT_FUNNEL_STAGES]
lost_stage = next(s for s in stages if s["code"] == lead_domain.LOST)
assert lost_stage["requires_reason"] == 1
print("  ok —", [(s["code"], s["kind"]) for s in stages])


print("\n== add_lead() без funnel_id уходит в дефолтную воронку, origin_channel — из source_type ==")
lead_id = db.add_lead(None, None, {"text": "хочу глицерин"}, status="new",
                       source_type=lead_domain.SOURCE_TYPE_MANUAL)
lead = db.get_lead(lead_id)
assert lead["funnel_id"] == default_funnel["id"]
assert lead["origin_channel"] == lead_domain.ORIGIN_CHANNEL_TELEGRAM
print("  ok")


print("\n== вторая воронка со своими кодами — в т.ч. совпадающими с первой ==")
# Своя тестовая воронка, не migration 020's seeded одноимённая — этому
# тесту (С10) нужна воронка с *произвольными* этапами/кодами, а не
# П9's конкретный набор, так что она просто заведена отдельно здесь и
# нигде не смешивается с той, что видна через get_funnel_by_channel().
mail_funnel_id = db.create_funnel("Почта · прямой запрос", lead_domain.ORIGIN_CHANNEL_EMAIL)
db.create_funnel_stage(mail_funnel_id, "new", "новое обращение", kind=lead_domain.KIND_OPEN,
                        color_bg="rgba(1,2,3,40)", color_fg="#111", color_dot="#222")
db.create_funnel_stage(mail_funnel_id, "invoiced", "выставлен счёт", kind=lead_domain.KIND_OPEN)
db.create_funnel_stage(mail_funnel_id, "shipped", "отгружено", kind=lead_domain.KIND_WON)
db.create_funnel_stage(mail_funnel_id, "declined", "отказ", kind=lead_domain.KIND_LOST,
                        requires_reason=True)
mail_stages = db.list_funnel_stages(mail_funnel_id)
assert [s["code"] for s in mail_stages] == ["new", "invoiced", "shipped", "declined"]
# "new" существует в обеих воронках с разным смыслом — не должно ничего
# путать: get_funnel_stage_by_code всегда даёт код в контексте funnel_id.
default_new = db.get_funnel_stage_by_code(default_funnel["id"], "new")
mail_new = db.get_funnel_stage_by_code(mail_funnel_id, "new")
assert default_new["id"] != mail_new["id"]
assert default_new["label"] != mail_new["label"]
print("  ok — код «new» в двух воронках — два разных этапа, не коллизия")


print("\n== заявка можно сразу создать в другой воронке ==")
mail_lead_id = db.add_lead(
    None, None, {"text": "нужен глицерин оптом"}, status="new",
    source_type=lead_domain.SOURCE_TYPE_MANUAL, funnel_id=mail_funnel_id,
    origin_channel=lead_domain.ORIGIN_CHANNEL_EMAIL,
)
mail_lead = db.get_lead(mail_lead_id)
assert mail_lead["funnel_id"] == mail_funnel_id
assert mail_lead["origin_channel"] == lead_domain.ORIGIN_CHANNEL_EMAIL
print("  ok")


print("\n== set_lead_status валидирует по своей воронке, requires_reason у своего этапа ==")
try:
    db.set_lead_status(mail_lead_id, "declined")
    raise AssertionError("должно было потребовать причину")
except ValueError as e:
    assert "причин" in str(e)
db.set_lead_status(mail_lead_id, "declined", reject_reason="не устроила цена")
assert db.get_lead(mail_lead_id)["reject_reason"] == "не устроила цена"
try:
    db.set_lead_status(mail_lead_id, "qualified")  # код из ДРУГОЙ воронки
    raise AssertionError("статус чужой воронки не должен приниматься")
except ValueError:
    pass
print("  ok — чужой код статуса и отсутствие причины отклоняются")


print("\n== next_stage идёт по своим этапам, не путается с чужой воронкой ==")
next_code = lead_domain.next_stage(mail_stages, "invoiced")
assert next_code == "shipped", next_code
print("  ok")


print("\n== перенос заявки между воронками: EVENT_KIND_FUNNEL, origin_channel не меняется ==")
db.set_lead_status(lead_id, "qualified")  # чтобы было что переносить, не «свежий new»
before_origin = db.get_lead(lead_id)["origin_channel"]
db.transfer_lead_funnel(lead_id, mail_funnel_id, "invoiced")
moved = db.get_lead(lead_id)
assert moved["funnel_id"] == mail_funnel_id
assert moved["status"] == "invoiced"
assert moved["origin_channel"] == before_origin, "origin_channel — атрибуция по первому касанию, перенос не должен её менять"
events = db.list_lead_events(lead_id)
funnel_events = [e for e in events if e["kind"] == lead_domain.EVENT_KIND_FUNNEL]
assert len(funnel_events) == 1, events
assert funnel_events[0]["from_status"] == "qualified" and funnel_events[0]["to_status"] == "invoiced"
print("  ok —", funnel_events[0]["text"])

try:
    db.transfer_lead_funnel(lead_id, mail_funnel_id, "нет-такого-этапа")
    raise AssertionError("несуществующий этап должен быть отклонён")
except ValueError:
    pass
try:
    db.transfer_lead_funnel(lead_id, 999999, "new")
    raise AssertionError("несуществующая воронка должна быть отклонена")
except ValueError:
    pass
print("  ok — перенос в несуществующую воронку/этап отклоняется")


print("\n== leads_status_counts/leads_funnel по умолчанию — только дефолтная воронка (обратная совместимость) ==")
counts = db.leads_status_counts()
assert set(counts.keys()) == {s["code"] for s in stages}
bucket = db.leads_funnel()
assert set(bucket.keys()) == {"new", "in_progress", "closed"}
mail_counts = db.leads_status_counts(funnel_id=mail_funnel_id)
assert mail_counts.get("invoiced", 0) >= 1
print("  ok — счётчики по умолчанию не задеты второй воронкой, но доступны по funnel_id явно")


print("\n== reject_reasons_report/leads_report_by_* считают по kind этапа, не по имени статуса ==")
db.set_lead_status(mail_lead_id, "declined", reject_reason="не устроила цена")
reasons = db.reject_reasons_report()
total_reasons = sum(r["c"] for r in reasons)
assert total_reasons >= 1, reasons  # mail_lead_id — единственный lost-лид на этот момент
by_source = db.leads_report_by_source()
by_direction = db.leads_report_by_direction()
total_all = sum(r["total"] for r in by_source)
assert total_all == len(db.list_leads()), (total_all, len(db.list_leads()))
won_total = sum(r["won"] for r in by_source)
lost_total = sum(r["lost"] for r in by_source)
assert lost_total >= 1, "хотя бы declined-лид из другой воронки должен посчитаться как lost по kind"
print("  ok — won/lost считаются по kind, включая лид из второй воронки")


print("\n== reorder_funnel_stages переставляет по заданному порядку ==")
ids_before = [s["id"] for s in db.list_funnel_stages(mail_funnel_id)]
new_order = [ids_before[1], ids_before[0], ids_before[2], ids_before[3]]
db.reorder_funnel_stages(mail_funnel_id, new_order)
ids_after = [s["id"] for s in db.list_funnel_stages(mail_funnel_id)]
assert ids_after == new_order, (ids_after, new_order)
print("  ok")


print("\n== delete_funnel_stage убирает этап, update_funnel_stage меняет поля ==")
extra_id = db.create_funnel_stage(mail_funnel_id, "extra", "лишний", kind=lead_domain.KIND_OPEN)
before_count = len(db.list_funnel_stages(mail_funnel_id))
db.delete_funnel_stage(extra_id)
assert len(db.list_funnel_stages(mail_funnel_id)) == before_count - 1
db.update_funnel_stage(ids_after[0], label="переименовано", requires_reason=True)
renamed = db.get_funnel_stage(ids_after[0])
assert renamed["label"] == "переименовано"
assert renamed["requires_reason"] == 1
print("  ok")


# ---- UI: FunnelsScreen, FunnelTransferDialog, LeadStatusPill ----------------
print("\n== UI офскрин: FunnelsScreen, FunnelTransferDialog, LeadStatusPill ==")
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMessageBox
app = QApplication.instance() or QApplication(sys.argv)

from chatgrab.ui.screens.bots.funnels_screen import FunnelsScreen
from chatgrab.ui.screens.bots.funnel_transfer_dialog import FunnelTransferDialog
from chatgrab.ui.widgets import LeadStatusPill


class _StubCtx:
    def __init__(self, database):
        self.db = database


stub_ctx = _StubCtx(db)

print("\n-- LeadStatusPill: стадия и None-заглушка --")
pill = LeadStatusPill()
assert pill.text() == "●  —"  # заглушка до первой реальной стадии
pill.set_stage(dict(default_new))
assert "новый" in pill.text()
print("  ok")

print("\n-- FunnelsScreen: список воронок, добавление воронки и этапа, реордер, удаление --")
screen = FunnelsScreen(stub_ctx, lambda *a, **kw: None)
screen.on_show()
# Три к этому моменту: 013's «Телеграм · биржа», 020's «Почта · прямой
# запрос», и mail_funnel_id, заведённая этим файлом чуть выше — не та
# же воронка, что 020's (см. комментарий при её создании), просто ещё
# одна с тем же каналом "email".
assert screen.funnel_list.count() == 3
screen.new_funnel_name.setText("Тестовая воронка")
screen._on_pick_color("#4f7cff")


def _fake_get_text(*a, **kw):
    return "manual", True


import chatgrab.ui.screens.bots.funnels_screen as funnels_screen_module
original_input_dialog = funnels_screen_module.QInputDialog.getText
funnels_screen_module.QInputDialog.getText = staticmethod(_fake_get_text)
try:
    screen._on_add_funnel()
finally:
    funnels_screen_module.QInputDialog.getText = original_input_dialog
assert screen.funnel_list.count() == 4
new_funnel = next(f for f in db.list_funnels() if f["name"] == "Тестовая воронка")
assert len(db.list_funnel_stages(new_funnel["id"])) == 1, "новая воронка должна сразу получить один этап"

screen.selected_funnel_id = new_funnel["id"]
screen._refresh_stages()
screen.new_stage_label.setText("Второй этап")
screen._on_add_stage()
assert screen.stage_table.rowCount() == 2
print("  ok — воронка и этап добавлены через настоящие виджеты")

print("\n-- FunnelsScreen: нельзя удалить последний этап воронки --")
solo_funnel_id = db.create_funnel("Одноэтапная", "test")
solo_stage_id = db.create_funnel_stage(solo_funnel_id, "only", "единственный", kind=lead_domain.KIND_OPEN)
screen.selected_funnel_id = solo_funnel_id
screen._refresh_stages()
warned = []
original_question = QMessageBox.information
QMessageBox.information = staticmethod(lambda *a, **kw: warned.append(1))
try:
    screen._on_delete_stage(solo_stage_id, "единственный")
finally:
    QMessageBox.information = original_question
assert warned, "должно было предупредить, а не удалить последний этап"
assert len(db.list_funnel_stages(solo_funnel_id)) == 1
print("  ok")

print("\n-- FunnelTransferDialog: авто-подсказка этапа того же kind, перенос --")
qualified_lead_id = db.add_lead(None, None, {}, status="qualified", source_type=lead_domain.SOURCE_TYPE_MANUAL)
dlg = FunnelTransferDialog(stub_ctx, db.get_lead(qualified_lead_id))
idx = dlg.funnel_combo.findData(mail_funnel_id)
assert idx >= 0
dlg.funnel_combo.setCurrentIndex(idx)
suggested_code = dlg.stage_combo.currentData()
suggested_stage = db.get_funnel_stage_by_code(mail_funnel_id, suggested_code)
assert suggested_stage["kind"] == lead_domain.KIND_OPEN, \
    "лид был на open-этапе — подсказка должна быть open-этапом новой воронки"
dlg._on_confirm()
transferred = db.get_lead(qualified_lead_id)
assert transferred["funnel_id"] == mail_funnel_id
assert transferred["status"] == suggested_code
print("  ok — авто-подсказка того же kind, подтверждение переносит по-настоящему")

print("\nТЕСТ ПРОЙДЕН: воронки настраиваются, вторая воронка живёт своей жизнью, "
      "перенос и отчёты работают правильно")
