// Copia al portapapeles el valor de cualquier botón con clase "dni-copy" al
// hacer click (el DNI viaja en data-copy). Muestra un "✓ copiado" breve como
// feedback. Es un <button> a propósito: así los handlers de click-en-fila
// (doc-modal.js) lo ignoran solos, sin necesitar stopPropagation.
(function () {
  function copiar(texto) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(texto);
    }
    var textarea = document.createElement("textarea");
    textarea.value = texto;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    try {
      document.execCommand("copy");
    } catch (err) {
      // Sin soporte de copiado: no hay nada razonable para hacer acá.
    }
    document.body.removeChild(textarea);
    return Promise.resolve();
  }

  document.addEventListener("click", function (e) {
    var boton = e.target.closest(".dni-copy");
    if (!boton) return;
    var valor = boton.getAttribute("data-copy") || boton.textContent.trim();
    if (!valor) return;
    copiar(valor).then(function () {
      document.querySelectorAll(".dni-copy.ultimo-copiado").forEach(function (b) {
        b.classList.remove("ultimo-copiado");
      });
      boton.classList.add("ultimo-copiado");
      boton.classList.add("copiado");
      clearTimeout(boton._copyTimeout);
      boton._copyTimeout = setTimeout(function () {
        boton.classList.remove("copiado");
      }, 1200);
    });
  });
})();
