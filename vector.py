"""Embed product fingerprint JSONB rows and emit SQL for vendor_item_fingerprint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from config import settings
from services.embedding_service import embed_text

FINGERPRINTS: List[Dict[str, Any]] = [
    {
        "size": "5 kg",
        "brand": "Aashirvaad",
        "product": "Aashirvaad 5 kg",
        "category": "Atta",
        "keywords": ["aashirvaad", "atta", "flour", "wheat flour", "5kg", "bag"],
        "pack_size": 4.0,
        "consumer_unit": "Bag",
        "purchase_unit": "Case",
    },
    {
        "size": "1 kg",
        "brand": "Surf Excel",
        "product": "Surf Excel 1 kg",
        "category": "Detergent",
        "keywords": ["surf excel", "detergent", "washing powder", "laundry", "1kg", "pouch"],
        "pack_size": 12.0,
        "consumer_unit": "Pouch",
        "purchase_unit": "Carton",
    },
    {
        "size": "250 g",
        "brand": "Rin",
        "product": "Rin 250 g",
        "category": "Detergent",
        "keywords": ["rin", "detergent bar", "washing soap", "250g"],
        "pack_size": 48.0,
        "consumer_unit": "Bar",
        "purchase_unit": "Carton",
    },
    {
        "size": "1 kg",
        "brand": "Wheel",
        "product": "Wheel 1 kg",
        "category": "Detergent",
        "keywords": ["wheel", "detergent", "washing powder", "laundry", "1kg"],
        "pack_size": 12.0,
        "consumer_unit": "Pouch",
        "purchase_unit": "Carton",
    },
    {
        "size": "100 g",
        "brand": "Colgate",
        "product": "Colgate 100 g",
        "category": "Oral Care",
        "keywords": ["colgate", "toothpaste", "oral care", "100g"],
        "pack_size": 72.0,
        "consumer_unit": "Tube",
        "purchase_unit": "Carton",
    },
    {
        "size": "150 g",
        "brand": "Closeup",
        "product": "Closeup 150 g",
        "category": "Oral Care",
        "keywords": ["closeup", "toothpaste", "oral care", "150g"],
        "pack_size": 48.0,
        "consumer_unit": "Tube",
        "purchase_unit": "Carton",
    },
    {
        "size": "100 g",
        "brand": "Britannia",
        "product": "Britannia 100 g",
        "category": "Biscuits",
        "keywords": ["britannia", "biscuit", "cookies", "100g"],
        "pack_size": 60.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
    {
        "size": "80 g",
        "brand": "Parle-G",
        "product": "Parle-G 80 g",
        "category": "Biscuits",
        "keywords": ["parle g", "glucose biscuit", "80g"],
        "pack_size": 96.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
    {
        "size": "100 g",
        "brand": "Good Day",
        "product": "Good Day 100 g",
        "category": "Biscuits",
        "keywords": ["good day", "cookies", "biscuit", "100g"],
        "pack_size": 60.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
    {
        "size": "100 g",
        "brand": "Marie Gold",
        "product": "Marie Gold 100 g",
        "category": "Biscuits",
        "keywords": ["marie gold", "tea biscuit", "100g"],
        "pack_size": 60.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
    {
        "size": "70 g",
        "brand": "Maggi",
        "product": "Maggi 70 g",
        "category": "Noodles",
        "keywords": ["maggi", "instant noodles", "70g"],
        "pack_size": 48.0,
        "consumer_unit": "Packet",
        "purchase_unit": "Carton",
    },
    {
        "size": "100 g",
        "brand": "Nescafe",
        "product": "Nescafe 100 g",
        "category": "Coffee",
        "keywords": ["nescafe", "coffee", "instant coffee", "100g"],
        "pack_size": 24.0,
        "consumer_unit": "Jar",
        "purchase_unit": "Carton",
    },
    {
        "size": "100 g",
        "brand": "Amul",
        "product": "Amul 100 g",
        "category": "Dairy",
        "keywords": ["amul", "butter", "dairy", "100g"],
        "pack_size": 30.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
    {
        "size": "500 ml",
        "brand": "Milma",
        "product": "Milma 500 ml",
        "category": "Dairy",
        "keywords": ["milma", "milk", "dairy", "500ml"],
        "pack_size": 20.0,
        "consumer_unit": "Packet",
        "purchase_unit": "Crate",
    },
    {
        "size": "750 ml",
        "brand": "Coca-Cola",
        "product": "Coca-Cola 750 ml",
        "category": "Beverages",
        "keywords": ["coca cola", "soft drink", "cola", "750ml"],
        "pack_size": 12.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Crate",
    },
    {
        "size": "750 ml",
        "brand": "Sprite",
        "product": "Sprite 750 ml",
        "category": "Beverages",
        "keywords": ["sprite", "soft drink", "lemon", "750ml"],
        "pack_size": 12.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Crate",
    },
    {
        "size": "750 ml",
        "brand": "Fanta",
        "product": "Fanta 750 ml",
        "category": "Beverages",
        "keywords": ["fanta", "orange drink", "soft drink", "750ml"],
        "pack_size": 12.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Crate",
    },
    {
        "size": "750 ml",
        "brand": "Pepsi",
        "product": "Pepsi 750 ml",
        "category": "Beverages",
        "keywords": ["pepsi", "cola", "soft drink", "750ml"],
        "pack_size": 12.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Crate",
    },
    {
        "size": "750 ml",
        "brand": "7UP",
        "product": "7UP 750 ml",
        "category": "Beverages",
        "keywords": ["7up", "lime drink", "soft drink", "750ml"],
        "pack_size": 12.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Crate",
    },
    {
        "size": "250 g",
        "brand": "Tata Tea",
        "product": "Tata Tea 250 g",
        "category": "Tea",
        "keywords": ["tata tea", "tea", "black tea", "250g"],
        "pack_size": 24.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
    {
        "size": "200 g",
        "brand": "Bru",
        "product": "Bru 200 g",
        "category": "Coffee",
        "keywords": ["bru", "coffee", "instant coffee", "200g"],
        "pack_size": 24.0,
        "consumer_unit": "Jar",
        "purchase_unit": "Carton",
    },
    {
        "size": "1 L",
        "brand": "Fortune",
        "product": "Fortune 1 L",
        "category": "Edible Oil",
        "keywords": ["fortune", "sunflower oil", "edible oil", "1l"],
        "pack_size": 15.0,
        "consumer_unit": "Pouch",
        "purchase_unit": "Carton",
    },
    {
        "size": "1 L",
        "brand": "Saffola",
        "product": "Saffola 1 L",
        "category": "Edible Oil",
        "keywords": ["saffola", "healthy oil", "edible oil", "1l"],
        "pack_size": 12.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Carton",
    },
    {
        "size": "500 ml",
        "brand": "Dabur",
        "product": "Dabur 500 ml",
        "category": "Personal Care",
        "keywords": ["dabur", "personal care", "500ml"],
        "pack_size": 24.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Carton",
    },
    {
        "size": "200 g",
        "brand": "Patanjali",
        "product": "Patanjali 200 g",
        "category": "FMCG",
        "keywords": ["patanjali", "fmcg", "200g"],
        "pack_size": 36.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
    {
        "size": "500 ml",
        "brand": "Harpic",
        "product": "Harpic 500 ml",
        "category": "Home Care",
        "keywords": ["harpic", "toilet cleaner", "500ml"],
        "pack_size": 12.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Carton",
    },
    {
        "size": "500 ml",
        "brand": "Lizol",
        "product": "Lizol 500 ml",
        "category": "Home Care",
        "keywords": ["lizol", "floor cleaner", "disinfectant", "500ml"],
        "pack_size": 12.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Carton",
    },
    {
        "size": "250 ml",
        "brand": "Dettol",
        "product": "Dettol 250 ml",
        "category": "Health",
        "keywords": ["dettol", "antiseptic", "disinfectant", "250ml"],
        "pack_size": 24.0,
        "consumer_unit": "Bottle",
        "purchase_unit": "Carton",
    },
    {
        "size": "200 g",
        "brand": "MTR",
        "product": "MTR 200 g",
        "category": "Food",
        "keywords": ["mtr", "ready to cook", "food", "200g"],
        "pack_size": 24.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
    {
        "size": "100 g",
        "brand": "Eastern",
        "product": "Eastern 100 g",
        "category": "Spices",
        "keywords": ["eastern", "spices", "masala", "100g"],
        "pack_size": 50.0,
        "consumer_unit": "Pack",
        "purchase_unit": "Carton",
    },
]


def jsonb_to_embedding_text(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def vector_literal(embedding: List[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def build_update_sql(
    fingerprint: Dict[str, Any],
    embedding: List[float],
    model: str,
) -> str:
    product = fingerprint.get("product", "?")
    fp_json = json.dumps(fingerprint, ensure_ascii=False)
    vec = vector_literal(embedding)
    return (
        f"-- {product}\n"
        f"UPDATE public.vendor_item_fingerprint\n"
        f"SET embedding = '{vec}'::vector,\n"
        f"    embedding_model = '{sql_escape(model)}'\n"
        f"WHERE fingerprint_json = '{sql_escape(fp_json)}'::jsonb;\n"
    )


def build_insert_sql(
    fingerprint: Dict[str, Any],
    embedding: List[float],
    model: str,
    vendor_item_id: int,
) -> str:
    product = fingerprint.get("product", "?")
    fp_json = json.dumps(fingerprint, ensure_ascii=False)
    vec = vector_literal(embedding)
    return (
        f"-- {product}\n"
        f"INSERT INTO public.vendor_item_fingerprint "
        f"(vendor_item_id, fingerprint_json, embedding, embedding_model)\n"
        f"VALUES (\n"
        f"  {vendor_item_id},\n"
        f"  '{sql_escape(fp_json)}'::jsonb,\n"
        f"  '{vec}'::vector,\n"
        f"  '{sql_escape(model)}'\n"
        f");\n"
    )


if __name__ == "__main__":
    out_dir = Path(__file__).resolve().parent
    update_path = out_dir / "vendor_item_fingerprint_embeddings_update.sql"
    insert_path = out_dir / "vendor_item_fingerprint_embeddings_insert.sql"
    model = settings.embedding_model

    update_parts: List[str] = [
        "-- Auto-generated: set embedding on existing vendor_item_fingerprint rows\n",
        "-- Matched by fingerprint_json\n\n",
        "BEGIN;\n\n",
    ]
    vendor_item_id_start = 226
    insert_parts: List[str] = [
        "-- Auto-generated: insert fingerprint + embedding rows\n",
        f"-- vendor_item_id values: {vendor_item_id_start}.."
        f"{vendor_item_id_start + len(FINGERPRINTS) - 1}\n\n",
        "BEGIN;\n\n",
    ]

    for i, fingerprint in enumerate(FINGERPRINTS):
        vendor_item_id = vendor_item_id_start + i
        product = fingerprint.get("product", f"item-{vendor_item_id}")
        print(
            f"[{i + 1}/{len(FINGERPRINTS)}] embedding {product} "
            f"(vendor_item_id={vendor_item_id}) ...",
            flush=True,
        )
        embedding = embed_text(jsonb_to_embedding_text(fingerprint))
        if len(embedding) != settings.embedding_dimensions:
            raise RuntimeError(
                f"{product}: expected {settings.embedding_dimensions} dims, got {len(embedding)}"
            )
        update_parts.append(build_update_sql(fingerprint, embedding, model))
        update_parts.append("\n")
        insert_parts.append(
            build_insert_sql(fingerprint, embedding, model, vendor_item_id)
        )
        insert_parts.append("\n")

    update_parts.append("COMMIT;\n")
    insert_parts.append("COMMIT;\n")

    update_path.write_text("".join(update_parts), encoding="utf-8")
    insert_path.write_text("".join(insert_parts), encoding="utf-8")

    print(f"\nWrote {update_path}")
    print(f"Wrote {insert_path}")
    print(f"Rows: {len(FINGERPRINTS)}, model: {model}, dims: {settings.embedding_dimensions}")
