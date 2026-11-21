# api/

Capa HTTP. Traduce requests/responses; nunca contiene reglas de negocio (eso vive en `src/services/`).

```
api/
└── v1/
    ├── deps.py          # inyectores: arman repository -> service para cada dominio
    └── routes/          # un archivo por dominio, registrado en main.py con su prefix
        ├── health.py     # GET /health
        ├── auth.py       # /auth/*        (Módulo A - verificación WhatsApp por OTP + login admin + login KDS por PIN implementados; SSO Entra ID sigue placeholder)
        ├── webhooks.py   # /webhooks/*    (Módulo B - placeholder + bridge a agent/)
        ├── orders.py     # /api/orders/*  (Módulo C - EJEMPLO COMPLETO)
        ├── restaurants.py # /api/restaurants/* (Módulo D - placeholder)
        ├── menus.py      # /api/menus/*   (Módulo D - implementado mínimo)
        ├── metrics.py    # /api/metrics/* (Módulo E - placeholder)
        └── kds_stream.py # /ws/kds/{restaurant_id} (WebSocket -- push de pedidos al KDS en tiempo real)
```

## Por qué `v1/`

Cuando haya cambios incompatibles en la API (ej. cambiar la forma de `OrderResponse`), se agrega `v2/` en paralelo en vez de romper a los clientes existentes (app KDS, integraciones). Todo el prefijo `/api/v1/...` no se usa literalmente en los endpoints actuales porque el diseño usa rutas planas (`/api/orders`, `/auth/login`, etc.) — el versionado vive en el nombre de la carpeta y el prefijo se registra en `main.py`, listo para cambiar a `/api/v2/...` el día que se necesite sin tocar el código de las rutas.

## Cómo agregar un endpoint nuevo

1. Define/reutiliza el schema en `src/schemas/<dominio>.py`.
2. Si necesitas negocio nuevo, agrégalo al `service` correspondiente en `src/services/`.
3. Agrega el inyector `get_<dominio>_service` en `deps.py` si no existe.
4. Escribe la función del endpoint en `routes/<dominio>.py`, usando `Depends(get_<dominio>_service)`.
5. Si es un dominio nuevo, registra el router en `main.py` con `app.include_router(...)`.

`orders.py` es el ejemplo a seguir para un flujo completo; `menus.py` muestra la misma idea aplicada a un segundo dominio.
