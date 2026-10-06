# -*- coding: utf-8 -*-
"""Ventanita para pasar a JDE los DNI de las solicitudes autorizadas, de a uno
con F8 (como copypaste/contador_mouse.py, pero integrado a esta app).

La abre app.py (botón "Pasar a JDE" en Solicitudes) con la lista armada en un
JSON: python pegador_jde.py <ruta.json>. F8 pega el DNI actual donde esté el
cursor (en JDE) y avanza al siguiente. Con ◀ / ▶ (o clic en la lista) se elige
por cuál seguir sin pegar.
"""
import ctypes
import json
import sys
import tkinter as tk
from tkinter import messagebox, ttk

import db

VK_F8 = 0x77
VK_CONTROL = 0x11
VK_V = 0x56
KEYEVENTF_KEYUP = 0x0002


def _ctrl_v():
    user32 = ctypes.windll.user32
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


class PegadorJDE:
    def __init__(self, root, items, usuario_id, usuario_nombre):
        self.root = root
        self.items = items
        self.usuario_id = usuario_id
        self.usuario_nombre = usuario_nombre
        self.index = 0
        self.pegados = set()

        root.title(f"Pasar a JDE · {len(items)} DNI")
        root.attributes("-topmost", True)
        root.geometry("380x520")
        root.minsize(320, 400)

        self.dni_var = tk.StringVar()
        self.nombre_var = tk.StringVar()
        self.pos_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Poné el cursor en JDE y apretá F8 para pegar el DNI actual.")

        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(main, text="DNI actual", font=("Segoe UI", 10)).pack(anchor="w")
        ttk.Label(main, textvariable=self.dni_var, font=("Segoe UI", 26, "bold")).pack(anchor="w")
        ttk.Label(main, textvariable=self.nombre_var, font=("Segoe UI", 10)).pack(anchor="w")
        ttk.Label(main, textvariable=self.pos_var, font=("Segoe UI", 11, "bold"), foreground="#1f4e8c").pack(
            anchor="w", pady=(4, 8)
        )

        nav = ttk.Frame(main)
        nav.pack(fill="x", pady=(0, 8))
        ttk.Button(nav, text="◀ Anterior", command=lambda: self.mover(-1)).pack(
            side="left", fill="x", expand=True, padx=(0, 4)
        )
        ttk.Button(nav, text="Siguiente ▶", command=lambda: self.mover(1)).pack(
            side="left", fill="x", expand=True, padx=(4, 0)
        )

        lista_frame = ttk.Frame(main)
        lista_frame.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(lista_frame, orient="vertical")
        self.lista = tk.Listbox(lista_frame, font=("Consolas", 10), activestyle="none",
                                exportselection=False, yscrollcommand=scroll.set)
        scroll.config(command=self.lista.yview)
        scroll.pack(side="right", fill="y")
        self.lista.pack(side="left", fill="both", expand=True)
        self.lista.bind("<<ListboxSelect>>", self._on_select)

        ttk.Label(main, textvariable=self.status_var, foreground="#1f6f43", wraplength=340).pack(
            anchor="w", pady=(8, 6)
        )
        ttk.Button(main, text="Marcar las pegadas como ejecutadas", command=self.marcar_ejecutadas).pack(fill="x")

        self._refrescar()
        GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState
        GetAsyncKeyState(VK_F8)  # descarta una pulsación previa a abrir la ventana
        self._poll_f8()

    def _texto_item(self, i):
        it = self.items[i]
        marca = "✓" if i in self.pegados else " "
        return f"{marca} {i + 1:>3}. {it['dni']:<10} {it['nombre'] or ''}"

    def _refrescar(self):
        self.lista.delete(0, "end")
        for i in range(len(self.items)):
            self.lista.insert("end", self._texto_item(i))
            if i in self.pegados:
                self.lista.itemconfig(i, foreground="#6b7280")
        if self.index < len(self.items):
            it = self.items[self.index]
            self.dni_var.set(it["dni"])
            self.nombre_var.set(it["nombre"] or "")
            self.pos_var.set(f"Vas por el {self.index + 1} de {len(self.items)} · pegados {len(self.pegados)}")
            self.lista.selection_clear(0, "end")
            self.lista.selection_set(self.index)
            self.lista.see(self.index)
        else:
            self.dni_var.set("—")
            self.nombre_var.set("")
            self.pos_var.set(f"Terminado · pegados {len(self.pegados)} de {len(self.items)}")

    def _on_select(self, _event=None):
        sel = self.lista.curselection()
        if sel and sel[0] != self.index:
            self.index = sel[0]
            self._refrescar()

    def mover(self, paso):
        self.index = max(0, min(len(self.items), self.index + paso))
        self._refrescar()

    def _poll_f8(self):
        # Bit 0: hubo una pulsación desde la última consulta (aunque el foco esté en JDE).
        if ctypes.windll.user32.GetAsyncKeyState(VK_F8) & 0x0001:
            self.pegar()
        self.root.after(25, self._poll_f8)

    def pegar(self):
        if self.index >= len(self.items):
            self.status_var.set("No quedan DNI: ya pasaste toda la lista.")
            return
        dni = self.items[self.index]["dni"]
        self.root.clipboard_clear()
        self.root.clipboard_append(dni)
        self.root.update()
        _ctrl_v()
        self.pegados.add(self.index)
        self.status_var.set(f"Pegado {dni}.")
        self.index += 1
        self._refrescar()

    def marcar_ejecutadas(self):
        if not self.pegados:
            messagebox.showinfo("Pasar a JDE", "Todavía no pegaste ninguno.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Pasar a JDE",
            f"¿Marcar como ejecutadas las {len(self.pegados)} solicitudes pegadas en JDE?",
            parent=self.root,
        ):
            return
        marcadas = 0
        for i in sorted(self.pegados):
            it = self.items[i]
            s = db.get_solicitud(it["id"])
            # Si mientras tanto alguien la rechazó o ya la marcó, no se toca.
            if not s or s["estado"] != "autorizado":
                continue
            db.ejecutar_solicitud(it["id"], self.usuario_id, self.usuario_nombre)
            db.log_evento(
                "solicitud",
                f"Solicitud #{it['id']} marcada como ejecutada (pasada a JDE con F8)",
                usuario={"id": self.usuario_id, "nombre_completo": self.usuario_nombre, "username": self.usuario_nombre},
            )
            marcadas += 1
        omitidas = len(self.pegados) - marcadas
        self.status_var.set(
            f"{marcadas} marcada(s) como ejecutada(s)"
            + (f"; {omitidas} ya no estaban autorizadas y no se tocaron." if omitidas else ".")
            + " Recargá Solicitudes en el navegador."
        )


def main():
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        cfg = json.load(f)
    root = tk.Tk()
    PegadorJDE(root, cfg["items"], cfg["usuario_id"], cfg["usuario_nombre"])
    root.mainloop()


if __name__ == "__main__":
    main()
