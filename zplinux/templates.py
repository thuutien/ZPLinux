"""Starting points offered on the welcome screen. Positions are for 203 dpi."""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

from .model import BoxElement, Code128Element, Label, QRElement, TextElement

Template = Tuple[str, str, str, Callable[[], Tuple[Label, Dict[str, str]]]]


def blank(width_mm: float, height_mm: float) -> Callable[[], Tuple[Label, Dict[str, str]]]:
    return lambda: (Label(width_mm=width_mm, height_mm=height_mm, dpi=203), {})


def product() -> Tuple[Label, Dict[str, str]]:
    lab = Label(width_mm=101.6, height_mm=50.8, dpi=203, elements=[
        BoxElement(x=10, y=10, w=793, h=386, thickness=4, rounding=1),
        TextElement(x=30, y=30, text="{name}", height=60),
        TextElement(x=30, y=100, text="SKU: {sku}", height=30),
        Code128Element(x=30, y=150, data="{sku}", height=120, module=3),
        QRElement(x=600, y=40, data="https://example.com/p/{sku}", magnification=6),
        BoxElement(x=560, y=300, w=220, h=70, filled=True),
        TextElement(x=560, y=315, text="{price}", height=45, block_width=220, align="C",
                    reverse=True),
    ])
    return lab, {"name": "Blue Mug", "sku": "MUG-0001", "price": "$12.99"}


def shipping() -> Tuple[Label, Dict[str, str]]:
    lab = Label(width_mm=101.6, height_mm=152.4, dpi=203, elements=[
        TextElement(x=30, y=25, text="FROM", height=22),
        TextElement(x=30, y=52, text="{from_name}\n{from_address}", height=26,
                    block_width=500, max_lines=3),
        BoxElement(x=20, y=150, w=773, h=4, thickness=4),
        TextElement(x=30, y=170, text="SHIP TO", height=28),
        TextElement(x=30, y=210, text="{to_name}\n{to_address}\n{to_city}", height=44,
                    block_width=750, max_lines=4),
        BoxElement(x=20, y=420, w=773, h=4, thickness=4),
        BoxElement(x=530, y=440, w=260, h=110, filled=True),
        TextElement(x=530, y=472, text="{service}", height=48, block_width=260, align="C",
                    reverse=True),
        TextElement(x=30, y=450, text="Weight: {weight}", height=34),
        TextElement(x=30, y=500, text="Ref: {reference}", height=34),
        BoxElement(x=20, y=570, w=773, h=4, thickness=4),
        TextElement(x=30, y=590, text="TRACKING #", height=26),
        Code128Element(x=60, y=630, data="{tracking}", height=230, module=3),
        QRElement(x=630, y=950, data="{tracking}", magnification=5),
        TextElement(x=30, y=1000, text="{tracking}", height=40),
    ])
    return lab, {"from_name": "Example Store", "from_address": "1 Warehouse Rd, Springfield",
                 "to_name": "Alex Doe", "to_address": "42 Main Street, Apt 5",
                 "to_city": "Riverside, CA 92501", "service": "PRIORITY", "weight": "1.2 kg",
                 "reference": "ORD-10045", "tracking": "1Z999AA10123456784"}


def address() -> Tuple[Label, Dict[str, str]]:
    lab = Label(width_mm=89, height_mm=28.6, dpi=203, elements=[
        TextElement(x=20, y=18, text="{name}\n{street}\n{city}", height=34,
                    block_width=670, max_lines=3),
    ])
    return lab, {"name": "Alex Doe", "street": "42 Main Street", "city": "Riverside, CA 92501"}


def asset_tag() -> Tuple[Label, Dict[str, str]]:
    lab = Label(width_mm=50.8, height_mm=25.4, dpi=203, elements=[
        QRElement(x=14, y=24, data="{asset}", magnification=6),
        TextElement(x=175, y=26, text="PROPERTY OF", height=20),
        TextElement(x=175, y=50, text="{company}", height=30, block_width=215),
        TextElement(x=175, y=110, text="{asset}", height=40),
    ])
    return lab, {"company": "Acme Inc.", "asset": "IT-00042"}


def price_tag() -> Tuple[Label, Dict[str, str]]:
    lab = Label(width_mm=57.15, height_mm=31.75, dpi=203, elements=[
        TextElement(x=16, y=14, text="{name}", height=30, block_width=425, max_lines=1),
        TextElement(x=16, y=52, text="{price}", height=80),
        Code128Element(x=16, y=150, data="{sku}", height=60, module=2, show_text=False),
        TextElement(x=300, y=200, text="{sku}", height=22),
    ])
    return lab, {"name": "Blue Mug", "price": "$12.99", "sku": "MUG-0001"}


# (id, title, size text, factory)
TEMPLATES: List[Template] = [
    ("blank-4x6", "Blank", "4 × 6 in", blank(101.6, 152.4)),
    ("blank-4x2", "Blank", "4 × 2 in", blank(101.6, 50.8)),
    ("blank-2x1", "Blank", "2 × 1 in", blank(50.8, 25.4)),
    ("shipping", "Shipping label", "4 × 6 in", shipping),
    ("product", "Product label", "4 × 2 in", product),
    ("price", "Price tag", "2.25 × 1.25 in", price_tag),
    ("address", "Address label", "3.5 × 1.125 in", address),
    ("asset", "Asset tag", "2 × 1 in", asset_tag),
]
