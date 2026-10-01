// Recuadro "Control GCG" de Solicitudes: si está corriendo, cuándo corrió por
// última vez y cuánto falta para la próxima. Refresca el estado del servidor cada
// 20 s y la cuenta regresiva cada segundo (con la hora del servidor como
// referencia, por si el reloj de la PC está corrido).
(function () {
  var box = document.getElementById("chequeo-gcg");
  if (!box) return;
  var estado = JSON.parse(box.dataset.estado);
  var desfase = 0; // hora del servidor - hora local, en ms

  function parse(iso) { return iso ? new Date(iso) : null; }
  function ahora() { return new Date(Date.now() + desfase); }
  function duracion(ms) {
    var s = Math.max(0, Math.round(ms / 1000));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), seg = s % 60;
    if (h) return h + " h " + m + " min";
    if (m) return m + " min" + (m < 5 ? " " + seg + " s" : "");
    return seg + " s";
  }
  function horaCorta(d) {
    var hoy = ahora().toDateString() === d.toDateString();
    var hhmm = d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
    return hoy ? hhmm : d.toLocaleDateString("es-AR", { day: "2-digit", month: "2-digit" }) + " " + hhmm;
  }
  function set(sel, txt) { box.querySelector(sel).textContent = txt; }

  function pintar() {
    var n = ahora();
    box.classList.toggle("corriendo", !!estado.corriendo);
    if (estado.corriendo) {
      set(".chequeo-estado", "corriendo ahora (hace " + duracion(n - parse(estado.inicio)) + ")");
    } else {
      set(".chequeo-estado", "en espera");
    }
    var ult = parse(estado.ultimo_fin);
    set(".chequeo-ultimo", ult ? "última: " + horaCorta(ult) + " (hace " + duracion(n - ult) + ")" : "última: sin registro");
    var prox = parse(estado.proxima);
    if (estado.corriendo) set(".chequeo-proximo", "próxima: " + estado.intervalo_min + " min después de que termine");
    else if (prox) set(".chequeo-proximo", prox > n ? "próxima: " + horaCorta(prox) + " (en " + duracion(prox - n) + ")" : "próxima: arrancando…");
    else set(".chequeo-proximo", "próxima: sin programar");
    box.title = estado.ultimo_resumen || "";
  }

  function refrescar() {
    fetch(box.dataset.url, { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d) return;
        estado = d;
        desfase = parse(d.ahora) - new Date();
        pintar();
      })
      .catch(function () {});
  }

  desfase = parse(estado.ahora) - new Date();
  pintar();
  setInterval(pintar, 1000);
  setInterval(refrescar, 20000);
})();
