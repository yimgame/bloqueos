// Ordena y filtra tablas del lado del cliente, sin recargar la página.
// Se activa en cualquier <table class="enhanced-table">: click en un
// encabezado ordena por esa columna (data-no-sort la excluye); si la tabla
// además tiene la clase "filterable" se agrega una fila de filtros de texto
// por columna (data-no-filter excluye una columna puntual).
// Las filas con la clase "total-row" quedan siempre fijas al final.
(function () {
  function normalizar(s) {
    return (s == null ? "" : String(s))
      .trim()
      .toLowerCase()
      .normalize("NFKD")
      .replace(/[̀-ͯ]/g, "");
  }

  function parseFecha(s) {
    var m = /^(\d{1,2})\/(\d{1,2})\/(\d{4})/.exec(s.trim());
    if (!m) return null;
    return new Date(+m[3], +m[2] - 1, +m[1]).getTime();
  }

  function parseNumero(s) {
    var t = s.trim();
    if (!t || !/^-?[\d.,]+$/.test(t)) return null;
    var n = parseFloat(t.replace(/\./g, "").replace(",", "."));
    return isNaN(n) ? null : n;
  }

  function valorCelda(fila, idx) {
    var celda = fila.children[idx];
    return celda ? celda.textContent.trim() : "";
  }

  function compararFilas(a, b, idx, dir) {
    var ta = valorCelda(a, idx);
    var tb = valorCelda(b, idx);
    var fa = parseFecha(ta);
    var fb = parseFecha(tb);
    if (fa !== null && fb !== null) return dir * (fa - fb);
    var na = parseNumero(ta);
    var nb = parseNumero(tb);
    if (na !== null && nb !== null) return dir * (na - nb);
    return dir * normalizar(ta).localeCompare(normalizar(tb));
  }

  function enhance(table) {
    var thead = table.tHead;
    var tbody = table.tBodies[0];
    if (!thead || !tbody) return;
    var headerRow = thead.rows[0];
    var headers = Array.prototype.slice.call(headerRow.cells);
    var filterable = table.classList.contains("filterable");
    var filterInputs = [];

    if (filterable) {
      var filterRow = document.createElement("tr");
      filterRow.className = "filter-row";
      headers.forEach(function (th, idx) {
        var celda = document.createElement("th");
        if (!th.hasAttribute("data-no-filter") && !th.hasAttribute("data-no-sort")) {
          var input = document.createElement("input");
          input.type = "text";
          input.className = "col-filter";
          input.placeholder = "Filtrar…";
          input.addEventListener("input", aplicarFiltros);
          celda.appendChild(input);
          filterInputs[idx] = input;
        }
        filterRow.appendChild(celda);
      });
      thead.appendChild(filterRow);
    }

    function aplicarFiltros() {
      var activos = [];
      filterInputs.forEach(function (input, idx) {
        if (input && input.value.trim()) {
          activos.push({ idx: idx, v: normalizar(input.value) });
        }
      });
      Array.prototype.forEach.call(tbody.rows, function (fila) {
        var visible = activos.every(function (f) {
          return normalizar(valorCelda(fila, f.idx)).indexOf(f.v) !== -1;
        });
        fila.hidden = !visible;
      });
    }

    headers.forEach(function (th, idx) {
      if (th.hasAttribute("data-no-sort")) return;
      th.classList.add("th-sortable");
      var flecha = document.createElement("span");
      flecha.className = "sort-indicator";
      th.appendChild(flecha);
      th.addEventListener("click", function () {
        var dir = th.getAttribute("data-dir") === "asc" ? -1 : 1;
        headers.forEach(function (t) {
          t.removeAttribute("data-dir");
          var ind = t.querySelector(".sort-indicator");
          if (ind) ind.textContent = "";
        });
        th.setAttribute("data-dir", dir === 1 ? "asc" : "desc");
        flecha.textContent = dir === 1 ? " ▲" : " ▼";

        var todas = Array.prototype.slice.call(tbody.rows);
        var fijas = todas.filter(function (f) { return f.classList.contains("total-row"); });
        var ordenables = todas.filter(function (f) { return !f.classList.contains("total-row"); });
        ordenables.sort(function (a, b) { return compararFilas(a, b, idx, dir); });
        ordenables.concat(fijas).forEach(function (f) { tbody.appendChild(f); });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table.enhanced-table").forEach(enhance);
  });
})();
