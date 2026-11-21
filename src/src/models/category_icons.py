"""
Iconos de categoría (accesibilidad en el KDS).

El backend solo guarda y valida la CLAVE del icono (`menu_categories.icon`); los
dibujos viven en el frontend (`packages/category-icons`). Si se agrega una clave
acá hay que agregar su dibujo allá -- el frontend muestra un icono genérico para
las claves que no conozca, así que un desfase nunca deja un hueco en pantalla.

`suggest_icon(nombre)` propone un icono a partir del nombre de la categoría
("Bebidas frías" -> "bebidas") para que todo tenga icono sin que nadie tenga
que elegirlo a mano.
"""

import re
import unicodedata

DEFAULT_CATEGORY_ICON = "otros"

# Orden = orden de presentación en el selector del panel.
CATEGORY_ICON_KEYS: tuple[str, ...] = (
    # Comidas del día
    "desayuno",
    "almuerzo",
    "cena",
    "combos",
    # Platos fuertes
    "hamburguesas",
    "hotdogs",
    "pizza",
    "burritos",
    "tacos",
    "sushi",
    "pollo",
    "carnes",
    "pescado",
    "pasta",
    "sopas",
    "arroz",
    "ensaladas",
    "sandwiches",
    # Panes y dulces
    "panes",
    "galletas",
    "postres",
    "helados",
    "dulces",
    # Snacks
    "snacks",
    "chucherias",
    "papas",
    "frutas",
    # Bebidas
    "bebidas",
    "cafe",
    "jugos",
    "licuados",
    "cerveza",
    # Otros
    "vegetariano",
    "extras",
    DEFAULT_CATEGORY_ICON,
)

# (clave, palabras clave). Se prueba en orden y gana la primera que coincida, así
# "Sopa de pollo" cae en sopas y no en pollo. Una palabra clave coincide como
# PREFIJO de una palabra del nombre; con "=" al inicio, solo como palabra exacta.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("combos", ("combo", "paquete", "promocion")),
    ("hotdogs", ("hot dog", "hotdog", "perro caliente", "salchicha", "shuco", "choripan")),
    ("hamburguesas", ("hamburguesa", "burger")),
    ("pizza", ("pizza",)),
    ("burritos", ("burrito", "wrap", "quesadilla")),
    ("tacos", ("taco", "tostada", "enchilada", "nacho", "fajita")),
    ("sushi", ("sushi", "maki", "nigiri", "sashimi")),
    ("sopas", ("sopa", "caldo", "crema", "ramen", "consome")),
    ("ensaladas", ("ensalada", "salad")),
    ("pasta", ("pasta", "espagueti", "spaghetti", "lasagna", "lasana", "fideo", "macarron")),
    ("sandwiches", ("sandwich", "sanduche", "emparedado", "torta", "baguette", "=sub")),
    ("arroz", ("arroz", "bowl", "poke")),
    ("pollo", ("pollo", "alita", "nugget", "broaster")),
    ("carnes", ("carne", "=res", "bistec", "parrilla", "asado", "churrasco", "cerdo")),
    ("pescado", ("pescado", "marisco", "camaron", "ceviche", "tilapia", "atun")),
    ("desayuno", ("desayuno", "huevo", "panqueque", "pancake", "waffle", "hotcake")),
    ("almuerzo", ("almuerzo", "plato", "menu del dia")),
    ("cena", ("cena",)),
    ("cafe", ("cafe", "capuchino", "cappuccino", "espresso", "latte", "moka", "mocha")),
    ("licuados", ("licuado", "batido", "smoothie", "frappe")),
    ("jugos", ("jugo", "naranjada", "limonada", "=fresco")),
    ("cerveza", ("cerveza", "alcohol", "vino", "coctel", "licor")),
    ("bebidas", ("bebida", "refresco", "gaseosa", "soda", "=agua", "infusion", "te frio", "te helado")),
    ("helados", ("helado", "nieve", "paleta", "raspado")),
    ("postres", ("postre", "pastel", "=pay", "flan", "tres leches", "cheesecake", "queque")),
    ("galletas", ("galleta", "cookie", "brownie", "muffin")),
    ("panes", ("=pan", "panes", "panader", "croissant", "bolillo")),
    ("dulces", ("dulce", "caramelo", "chocolate", "gomita", "golosina", "confite")),
    ("chucherias", ("chucheria", "=chuchu")),
    ("papas", ("=papas", "papa frita", "fritura", "=fritas", "=chips")),
    ("snacks", ("snack", "palomita", "boquita")),
    ("frutas", ("fruta", "manzana", "banano", "platano", "papaya", "melon", "sandia", "fresa")),
    ("vegetariano", ("vegetariano", "vegano", "vegetal", "verdura", "saludable")),
    ("extras", ("extra", "adicional", "complemento", "salsa", "aderezo", "acompanamiento", "guarnicion", "topping")),
)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").strip()


def _compile(keyword: str) -> re.Pattern[str]:
    if keyword.startswith("="):
        return re.compile(rf"(?<![a-z0-9]){re.escape(keyword[1:])}(?![a-z0-9])")
    return re.compile(rf"(?<![a-z0-9]){re.escape(keyword)}")


_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = tuple(
    (key, tuple(_compile(k) for k in keywords)) for key, keywords in _KEYWORDS
)


def is_valid_icon(icon: str | None) -> bool:
    return icon in CATEGORY_ICON_KEYS


def suggest_icon(name: str | None) -> str:
    """Icono sugerido para el nombre de una categoría; `otros` si nada coincide."""
    normalized = _normalize(name or "")
    if not normalized:
        return DEFAULT_CATEGORY_ICON
    for key, patterns in _PATTERNS:
        if any(p.search(normalized) for p in patterns):
            return key
    return DEFAULT_CATEGORY_ICON
