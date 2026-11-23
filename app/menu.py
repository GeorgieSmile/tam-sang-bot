"""Menu loading, availability checks, and deterministic pricing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml

Fulfillment = Literal["pickup", "delivery"]
OverrideMap = dict[tuple[str, str], bool]


class MenuError(ValueError):
    """Raised when a requested dish, option, or quantity is invalid or sold out."""


@dataclass(frozen=True)
class DeliveryRule:
    fee: int
    free_above: int

    def calculate(self, food_total: int, fulfillment: Fulfillment) -> int:
        if fulfillment != "delivery" or self.fee <= 0 or food_total <= 0:
            return 0
        return 0 if food_total >= self.free_above else self.fee

    def rule_text_th(self) -> str:
        if self.fee <= 0:
            return "ส่งฟรี"
        return f"ค่าส่ง {self.fee} บาท (สั่งครบ {self.free_above} บาท ส่งฟรี)"


@dataclass(frozen=True)
class ShopConfig:
    name: str
    timezone: str
    open_time: str
    close_time: str
    delivery: DeliveryRule

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def hours_text_th(self) -> str:
        return f"{self.open_time}–{self.close_time} น."


@dataclass(frozen=True)
class Option:
    id: str
    name_th: str
    ingredient_key: str
    price_delta: int


@dataclass(frozen=True)
class OptionGroup:
    id: str
    name_th: str
    required: bool
    min_choices: int
    max_choices: int
    default: str | None
    options: tuple[Option, ...]


@dataclass(frozen=True)
class MenuItem:
    id: str
    name_th: str
    aliases: tuple[str, ...]
    category: str
    base_price: int
    groups: tuple[OptionGroup, ...]


@dataclass(frozen=True)
class PricedLine:
    dish_id: str
    dish_name_th: str
    qty: int
    unit_price: int
    line_total: int
    option_ids: tuple[str, ...]
    option_names_th: tuple[str, ...]
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "dish_id": self.dish_id,
            "dish_name_th": self.dish_name_th,
            "qty": self.qty,
            "unit_price": self.unit_price,
            "line_total": self.line_total,
            "option_ids": list(self.option_ids),
            "option_names_th": list(self.option_names_th),
            "note": self.note,
        }


@dataclass(frozen=True)
class PricedCart:
    lines: tuple[PricedLine, ...]
    food_total: int
    delivery_fee: int
    total: int


class Menu:
    def __init__(
        self,
        shop: ShopConfig,
        ingredients: dict[str, str],
        items: tuple[MenuItem, ...],
    ) -> None:
        self.shop = shop
        self.ingredients = ingredients
        self.items = items
        self.items_by_id: dict[str, MenuItem] = {item.id: item for item in items}

    def is_scheduled_open(self, now: datetime) -> bool:
        local = now.astimezone(self.shop.tz)
        hm = local.strftime("%H:%M")
        open_hm, close_hm = self.shop.open_time, self.shop.close_time
        if open_hm <= close_hm:
            return open_hm <= hm < close_hm
        return hm >= open_hm or hm < close_hm

    def is_dish_available(self, dish_id: str, overrides: OverrideMap) -> bool:
        return overrides.get(("dish", dish_id), True)

    def is_ingredient_available(self, ingredient_key: str, overrides: OverrideMap) -> bool:
        return overrides.get(("ingredient", ingredient_key), True)

    def price_line(
        self,
        dish_id: str,
        qty: int,
        option_ids: list[str] | tuple[str, ...],
        note: str,
        overrides: OverrideMap,
    ) -> PricedLine:
        if qty < 1 or qty > 20:
            raise MenuError("จำนวนต้องอยู่ระหว่าง 1 ถึง 20 ค่ะ")
        item = self.items_by_id.get(dish_id)
        if item is None:
            raise MenuError(f"ไม่พบเมนู '{dish_id}' ในระบบค่ะ")
        if not self.is_dish_available(item.id, overrides):
            raise MenuError(f"{item.name_th} หมดแล้วค่ะ")

        if len(set(option_ids)) != len(option_ids):
            raise MenuError(f"มีตัวเลือกซ้ำในรายการ {item.name_th} ค่ะ")

        opt_lookup: dict[str, tuple[OptionGroup, Option]] = {}
        for group in item.groups:
            for opt in group.options:
                opt_lookup[opt.id] = (group, opt)

        chosen_by_group: dict[str, list[Option]] = {g.id: [] for g in item.groups}
        for oid in option_ids:
            found = opt_lookup.get(oid)
            if found is None:
                raise MenuError(f"ตัวเลือก '{oid}' ใช้กับ {item.name_th} ไม่ได้ค่ะ")
            group, opt = found
            chosen_by_group[group.id].append(opt)

        selected: list[Option] = []
        for group in item.groups:
            picks = chosen_by_group[group.id]
            if not picks and group.default is not None:
                default_opt = next((o for o in group.options if o.id == group.default), None)
                if default_opt is not None:
                    picks = [default_opt]

            if len(picks) < group.min_choices:
                raise MenuError(f"{item.name_th} ต้องเลือก{group.name_th}ด้วยค่ะ")
            if len(picks) > group.max_choices:
                raise MenuError(
                    f"{item.name_th} เลือก{group.name_th}ได้ไม่เกิน {group.max_choices} อย่างค่ะ"
                )

            for opt in picks:
                if not self.is_ingredient_available(opt.ingredient_key, overrides):
                    ing_name = self.ingredients.get(opt.ingredient_key, opt.name_th)
                    raise MenuError(f"{ing_name} หมดแล้วค่ะ")
                selected.append(opt)

        unit_price = item.base_price + sum(opt.price_delta for opt in selected)
        return PricedLine(
            dish_id=item.id,
            dish_name_th=item.name_th,
            qty=qty,
            unit_price=unit_price,
            line_total=unit_price * qty,
            option_ids=tuple(opt.id for opt in selected),
            option_names_th=tuple(opt.name_th for opt in selected),
            note=note.strip(),
        )

    def price_cart(
        self,
        raw_items: list[dict[str, Any]],
        fulfillment: Fulfillment,
        overrides: OverrideMap,
    ) -> PricedCart:
        if not raw_items:
            raise MenuError("ยังไม่มีรายการอาหารในตะกร้าค่ะ")
        if len(raw_items) > 20:
            raise MenuError("สั่งได้ไม่เกิน 20 รายการต่อออเดอร์ค่ะ")
        lines = tuple(
            self.price_line(
                dish_id=str(line["dish_id"]),
                qty=int(line.get("qty", 1)),
                option_ids=[str(o) for o in line.get("option_ids", [])],
                note=str(line.get("note", "")),
                overrides=overrides,
            )
            for line in raw_items
        )
        food_total = sum(line.line_total for line in lines)
        delivery_fee = self.shop.delivery.calculate(food_total, fulfillment)
        return PricedCart(
            lines=lines,
            food_total=food_total,
            delivery_fee=delivery_fee,
            total=food_total + delivery_fee,
        )

    def sold_out_summary(self, overrides: OverrideMap) -> dict[str, list[str]]:
        sold_ingredients = [
            name
            for key, name in self.ingredients.items()
            if not self.is_ingredient_available(key, overrides)
        ]
        sold_dishes = [
            item.name_th for item in self.items if not self.is_dish_available(item.id, overrides)
        ]
        return {"ingredients": sold_ingredients, "dishes": sold_dishes}

    def to_prompt_dict(self, overrides: OverrideMap) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in self.items:
            groups_out: list[dict[str, Any]] = []
            for group in item.groups:
                opts_out = [
                    {
                        "id": o.id,
                        "name": o.name_th,
                        "delta": o.price_delta,
                        "available": self.is_ingredient_available(o.ingredient_key, overrides),
                    }
                    for o in group.options
                ]
                groups_out.append(
                    {
                        "id": group.id,
                        "name": group.name_th,
                        "required": group.required,
                        "default": group.default,
                        "options": opts_out,
                    }
                )
            result.append(
                {
                    "id": item.id,
                    "name": item.name_th,
                    "aliases": list(item.aliases),
                    "category": item.category,
                    "price": item.base_price,
                    "available": self.is_dish_available(item.id, overrides),
                    "groups": groups_out,
                }
            )
        return result

    def find_target(self, query_th: str) -> tuple[Literal["ingredient", "dish"], str, str] | None:
        """Match a Thai ingredient or dish name/alias to an override target."""
        q = query_th.strip().replace(" ", "")
        if not q:
            return None
        # Exact ingredient match first (longest match wins)
        ing_matches: list[tuple[int, str, str]] = []
        for key, name_th in self.ingredients.items():
            clean = name_th.replace(" ", "")
            if q == clean or clean in q:
                ing_matches.append((len(clean), key, name_th))
        # Exact/alias dish matches
        dish_matches: list[tuple[int, str, str]] = []
        for item in self.items:
            names = [item.name_th, *item.aliases]
            for name in names:
                clean = name.replace(" ", "")
                if q == clean or clean in q:
                    dish_matches.append((len(clean), item.id, item.name_th))
        # Prefer exact dish match when user names a full dish (e.g. ข้าวไข่เจียวหมูสับ)
        best_dish = max(dish_matches, default=None, key=lambda x: x[0])
        best_ing = max(ing_matches, default=None, key=lambda x: x[0])
        if best_dish and (best_ing is None or best_dish[0] > best_ing[0]):
            return ("dish", best_dish[1], best_dish[2])
        if best_ing:
            return ("ingredient", best_ing[1], best_ing[2])
        return None


def load_menu(path: Path) -> Menu:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    shop_raw = raw["shop"]
    delivery_raw = shop_raw.get("delivery", {})
    if "delivery_fee" in raw:
        fee_val = int(raw["delivery_fee"])
    elif "delivery_fee" in shop_raw:
        fee_val = int(shop_raw["delivery_fee"])
    else:
        fee_val = int(delivery_raw.get("fee", 0))
    shop = ShopConfig(
        name=str(shop_raw["name"]),
        timezone=str(shop_raw.get("timezone", "Asia/Bangkok")),
        open_time=str(shop_raw["hours"]["open"]),
        close_time=str(shop_raw["hours"]["close"]),
        delivery=DeliveryRule(
            fee=fee_val,
            free_above=int(delivery_raw.get("free_above", 0)),
        ),
    )

    ingredients: dict[str, str] = {
        str(k): str(v) for k, v in (raw.get("ingredients") or {}).items()
    }

    group_defs: dict[str, dict[str, Any]] = raw.get("option_groups") or {}
    items: list[MenuItem] = []
    for item_raw in raw["items"]:
        allowed_proteins = item_raw.get("allowed_proteins")
        item_groups: list[OptionGroup] = []
        for group_id in item_raw.get("groups") or []:
            gdef = group_defs[group_id]
            opts: list[Option] = []
            for o in gdef["options"]:
                if group_id == "protein" and allowed_proteins is not None:
                    if o["id"] not in allowed_proteins:
                        continue
                opts.append(
                    Option(
                        id=str(o["id"]),
                        name_th=str(o["name_th"]),
                        ingredient_key=str(o["ingredient_key"]),
                        price_delta=int(o["price_delta"]),
                    )
                )
            item_groups.append(
                OptionGroup(
                    id=str(group_id),
                    name_th=str(gdef["name_th"]),
                    required=bool(gdef.get("required", False)),
                    min_choices=int(gdef.get("min_choices", 0)),
                    max_choices=int(gdef.get("max_choices", 1)),
                    default=str(gdef["default"]) if gdef.get("default") is not None else None,
                    options=tuple(opts),
                )
            )
        items.append(
            MenuItem(
                id=str(item_raw["id"]),
                name_th=str(item_raw["name_th"]),
                aliases=tuple(str(a) for a in (item_raw.get("aliases") or [])),
                category=str(item_raw["category"]),
                base_price=int(item_raw["base_price"]),
                groups=tuple(item_groups),
            )
        )
    return Menu(shop=shop, ingredients=ingredients, items=tuple(items))
