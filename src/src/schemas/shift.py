"""Schemas de respuesta de turnos KDS (inicio/cierre y resúmenes)."""

from pydantic import BaseModel, Field

from src.core.timeutils import LocalDateTime


class ShiftActorOut(BaseModel):
    type: str
    id: str | None = None
    username: str | None = None


class PauseOut(BaseModel):
    started_at: LocalDateTime
    ended_at: LocalDateTime | None = None
    hora_inicio: str
    hora_fin: str | None = None
    duracion_min: float
    started_by: ShiftActorOut | None = None
    ended_by: ShiftActorOut | None = None


class PausesOut(BaseModel):
    cantidad: int
    tiempo_total_min: float
    en_pausa: bool = Field(description="true = la estación está en pausa ahora (turno abierto)")
    detalle: list[PauseOut]


class ShiftOut(BaseModel):
    id: str
    restaurant_id: str | None = None
    kds_user_id: str
    kds_username: str | None = None
    local_date: str = Field(description="Fecha de inicio del turno (YYYY-MM-DD, hora de Guatemala)")
    shift_no: int = Field(description="Número del turno de la estación en `local_date` (1, 2, ...)")
    label: str = Field(description='Texto listo para mostrar: "Turno 2 · 14/10/2026"')
    status: str = Field(description="open | closed")
    started_at: LocalDateTime
    ended_at: LocalDateTime | None = None
    hora_inicio: str
    hora_fin: str | None = None
    duracion_min: int
    tiempo_activo_min: float = Field(description="duracion_min menos el tiempo en pausa")
    pausas: PausesOut
    stale: bool = Field(description="true = sigue abierto desde un día anterior")
    end_adjusted: bool = Field(description="true = se cerró después de su día; la hora final es el cierre del restaurante")
    started_by: ShiftActorOut | None = None
    ended_by: ShiftActorOut | None = None


class ShiftKpisOut(BaseModel):
    total_tickets: int
    aceptadas: int
    rechazadas: int
    rechazadas_carga: int
    rechazadas_stock: int
    rechazadas_sin_respuesta: int
    rechazadas_sin_motivo: int
    pendientes: int
    esperando_pago: int
    en_curso: int
    entregadas: int
    no_pagadas: int
    canceladas: int
    ordenes_pagadas: int
    total_dinero: float
    ticket_promedio: float
    pct_aceptadas: float
    pct_rechazadas: float
    pct_rechazadas_carga: float
    pct_rechazadas_stock: float
    pct_rechazadas_sin_respuesta: float
    tiempo_preparacion_promedio_min: float | None = None


class DayKpisOut(ShiftKpisOut):
    sin_turno: int = Field(description="Tickets que no caen dentro de ningún turno")


class TicketShiftRef(BaseModel):
    shift_id: str
    shift_no: int
    label: str


class ShiftTicketOut(BaseModel):
    order_id: str
    request_number: int | None = None
    order_number: int | None = None
    created_at: LocalDateTime
    hora: str
    items: str
    total_amount: float
    pipeline_status: str
    payment_status: str
    status_label: str
    resultado: str
    resultado_label: str
    aceptada: bool
    reject_reason: str | None = None
    out_of_stock_items: list[dict] = Field(default_factory=list)
    kds_user_id: str | None = None
    kds_username: str | None = None
    turno: TicketShiftRef | None = None


class PendingOut(BaseModel):
    por_aceptar: int
    en_curso: int


class CurrentShiftOut(BaseModel):
    shift: ShiftOut | None = None
    station_active: bool
    pendientes: PendingOut
    kpis: ShiftKpisOut | None = None


class ShiftStartOut(BaseModel):
    shift: ShiftOut
    station_active: bool
    previous_shift_closed: ShiftOut | None = None


class ShiftKpisItemOut(BaseModel):
    shift: ShiftOut
    kpis: ShiftKpisOut


class ShiftSummaryOut(ShiftKpisItemOut):
    tickets: list[ShiftTicketOut]
    pendientes: PendingOut | None = None


class DaySummaryOut(BaseModel):
    date: str
    restaurant_id: str
    kds_user_id: str | None = None
    kpis: DayKpisOut
    turnos: list[ShiftKpisItemOut]
    tickets: list[ShiftTicketOut]
