// Modal que muestra, para una solicitud de desbloqueo, el detalle de documentos
// tal como se vieron en la consulta a GCG guardada en esa solicitud (el JSON
// completo viaja embebido en un <script type="application/json"> por fila,
// generado por solicitudes.html).
(function () {
  function escapeHtml(s) {
    return (s == null ? "" : String(s)).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function formatearFechaHora(iso) {
    if (!iso) return "";
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso);
    if (!m) return iso;
    return m[3] + "/" + m[2] + "/" + m[1] + " " + m[4] + ":" + m[5];
  }

  function iconoEstado(estado) {
    if (estado === true) return '<span class="gcg-estado-icon gcg-estado-ok" title="Vigente">&#10003;</span>';
    if (estado === false) return '<span class="gcg-estado-icon gcg-estado-vencido" title="Vencido">&#10005;</span>';
    return '<span class="gcg-estado-icon gcg-estado-na" title="Sin dato">—</span>';
  }

  function filasDocumentos(documentos, formatearFecha) {
    if (!documentos.length) {
      return '<tr><td colspan="3" class="muted">No hay documentos para mostrar.</td></tr>';
    }
    return documentos
      .map(function (d) {
        return (
          "<tr><td>" + escapeHtml(d.nombre) + "</td>" +
          "<td>" + escapeHtml(formatearFecha(d) || "—") + "</td>" +
          "<td>" + iconoEstado(d.estado) + "</td></tr>"
        );
      })
      .join("");
  }

  function mostrarExtrasGcg(raw) {
    var bloqueContratista = document.getElementById("doc-modal-contratista");
    var bloqueRaw = document.getElementById("doc-modal-raw");
    if (!bloqueContratista || !bloqueRaw) return;

    var contratista = raw && raw.contratista;
    bloqueContratista.hidden = !contratista;
    if (contratista) {
      document.getElementById("doc-modal-contratista-title").textContent =
        "Contratista: " + [contratista.nombre, contratista.cuit ? "CUIT " + contratista.cuit : null].filter(Boolean).join(" · ");
      document.getElementById("doc-modal-contratista-meta").textContent = [
        contratista.iso ? "País: " + contratista.iso : null,
        contratista.estadoHabilitacion ? "Habilitación: " + contratista.estadoHabilitacion : null,
        contratista.fechaVto ? "Vto: " + String(contratista.fechaVto).replace(/-/g, "/") : null,
      ].filter(Boolean).join(" · ");
      document.getElementById("doc-modal-contratista-body").innerHTML = filasDocumentos(
        contratista.documentos || [],
        function (d) { return d.fechaVto ? String(d.fechaVto).replace(/-/g, "/") : null; }
      );
    }

    bloqueRaw.hidden = !raw;
    bloqueRaw.open = false;
    document.getElementById("doc-modal-raw-pre").textContent = raw ? JSON.stringify(raw, null, 2) : "";
  }

  function abrirModal(payload, fallback) {
    var modal = document.getElementById("doc-modal");
    var titulo = document.getElementById("doc-modal-title");
    var meta = document.getElementById("doc-modal-meta");
    var tbody = document.getElementById("doc-modal-body");
    if (!modal || !titulo || !meta || !tbody) return;

    fallback = fallback || {};
    var trabajador = payload.trabajador || {};
    var nombre = trabajador.nombre || fallback.nombre;
    var dni = trabajador.dni || fallback.dni;
    titulo.textContent = [nombre, dni ? "DNI " + dni : null].filter(Boolean).join(" · ") || "Documentos en GCG";

    var metaTxt = [];
    var raw = payload.raw;
    if (payload.consultado_en) metaTxt.push("Consultado en GCG: " + formatearFechaHora(payload.consultado_en));
    if (raw && raw.iso) metaTxt.push("País: " + raw.iso);
    if (raw && raw.estadoHabilitacionFinal) metaTxt.push("Habilitación final: " + raw.estadoHabilitacionFinal);
    if (payload.error) metaTxt.push("Error al consultar GCG: " + payload.error);
    meta.textContent = metaTxt.join(" · ") || "Sin datos de consulta a GCG para esta solicitud.";

    tbody.innerHTML = filasDocumentos(payload.documentos || [], function (d) { return d.fecha; });
    mostrarExtrasGcg(raw);
    modal.hidden = false;
  }

  function cerrarModal() {
    var modal = document.getElementById("doc-modal");
    if (modal) modal.hidden = true;
  }

  document.addEventListener("click", function (e) {
    if (e.target.id === "doc-modal" || e.target.id === "doc-modal-close") {
      cerrarModal();
      return;
    }
    // No interferir con los botones de Autorizar/Rechazar/Ejecutar ni con
    // los filtros/orden de la tabla: sólo abre el modal si el click cayó
    // fuera de cualquier elemento interactivo propio.
    if (e.target.closest("button, a, input, textarea, select, form, details")) return;

    var fila = e.target.closest("tr[data-doc-target]");
    if (!fila) return;
    var script = document.getElementById(fila.getAttribute("data-doc-target"));
    if (!script) return;
    var fallback = { dni: fila.getAttribute("data-dni"), nombre: fila.getAttribute("data-nombre") };
    try {
      abrirModal(JSON.parse(script.textContent), fallback);
    } catch (err) {
      // JSON inválido: no hay nada razonable para mostrar.
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") cerrarModal();
  });
})();
