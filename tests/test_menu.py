"""Behavioral tests for menu loading, pricing, smart defaults, and availability."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.menu import MenuError, load_menu

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")


def test_menu_seeds_24_items_and_prices_kaphrao_two_boxes() -> None:
    menu = load_menu(MENU_PATH)
    assert len(menu.items) == 24

    raw_items = [
        {
            "dish_id": "kaphrao",
            "qty": 2,
            "option_ids": ["crispy_pork", "fried_egg_runny"],
            "note": "ไม่ใส่ถั่วฝักยาว",
        }
    ]
    for fulfillment in ("pickup", "delivery"):
        priced = menu.price_cart(raw_items, fulfillment, {})
        assert priced.food_total == 140
        assert priced.delivery_fee == 0
        assert priced.total == 140
        # Default spice (เผ็ดปกติ) applied automatically
        assert "spice_normal" in priced.lines[0].option_ids
        assert priced.lines[0].note == "ไม่ใส่ถั่วฝักยาว"


def test_delivery_fee_under_100_vs_pickup_vs_zero_fee_yaml(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    one_box = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork", "fried_egg_runny"]}]

    delivery = menu.price_cart(one_box, "delivery", {})
    assert delivery.food_total == 70
    assert delivery.delivery_fee == 10
    assert delivery.total == 80

    pickup = menu.price_cart(one_box, "pickup", {})
    assert pickup.food_total == 70
    assert pickup.delivery_fee == 0
    assert pickup.total == 70

    # Setting delivery_fee: 0 in YAML disables delivery fees
    zero_yaml = tmp_path / "menu_free.yaml"
    zero_yaml.write_text(
        "delivery_fee: 0\n" + MENU_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    free_menu = load_menu(zero_yaml)
    free_delivery = free_menu.price_cart(one_box, "delivery", {})
    assert free_delivery.delivery_fee == 0
    assert free_delivery.total == 70


def test_smart_defaults_and_required_options() -> None:
    menu = load_menu(MENU_PATH)
    # Thai tea defaults sweetness -> sweetness_normal
    tea = menu.price_line("thai_tea", 1, [], "", {})
    assert tea.option_ids == ("sweetness_normal",)
    assert tea.unit_price == 35

    # Missing required protein fails cleanly
    with pytest.raises(MenuError, match="เนื้อสัตว์"):
        menu.price_line("kaphrao", 1, [], "", {})

    # Special portion (+10) works on meals, rejected on drinks
    special_meal = menu.price_line("pad_see_ew", 1, ["sliced_pork", "special"], "", {})
    assert special_meal.unit_price == 65
    with pytest.raises(MenuError):
        menu.price_line("thai_tea", 1, ["special"], "", {})


def test_ingredient_sold_out_disables_across_dishes_while_other_proteins_stay_open() -> None:
    menu = load_menu(MENU_PATH)
    overrides = {("ingredient", "crispy_pork"): False}

    for dish_id in ("kaphrao", "curry_paste", "kale"):
        with pytest.raises(MenuError, match="หมูกรอบ"):
            menu.price_line(dish_id, 1, ["crispy_pork"], "", overrides)
        ok_line = menu.price_line(dish_id, 1, ["sliced_pork"], "", overrides)
        assert ok_line.dish_id == dish_id


def test_schedule_hours() -> None:
    menu = load_menu(MENU_PATH)
    assert menu.is_scheduled_open(datetime(2026, 11, 23, 9, 0, tzinfo=BKK)) is True
    assert menu.is_scheduled_open(datetime(2026, 11, 23, 19, 59, tzinfo=BKK)) is True
    assert menu.is_scheduled_open(datetime(2026, 11, 23, 21, 0, tzinfo=BKK)) is False
    assert menu.is_scheduled_open(datetime(2026, 11, 23, 8, 59, tzinfo=BKK)) is False
