/* Orden de tablas en el navegador: clic en un título ordena (otro clic invierte).
   Se aplica a todas las tablas con encabezado, salvo las marcadas class="no-sort" (las que ordena el servidor
   porque están paginadas) y las matrices de colores. */
(function () {
  var coll = new Intl.Collator('es', { numeric: true, sensitivity: 'base' });

  function valor(td) {
    if (!td) return { t: 's', v: '' };
    if (td.dataset.sort !== undefined) { var n = parseFloat(td.dataset.sort); return isNaN(n) ? { t: 's', v: td.dataset.sort } : { t: 'n', v: n }; }
    var s = (td.textContent || '').replace(/\s+/g, ' ').trim();
    var f = s.match(/(\d{2})\/(\d{2})\/(\d{4})/);
    if (f) return { t: 'n', v: Date.UTC(+f[3], +f[2] - 1, +f[1]) };
    var num = s.replace(/[%,\s]/g, '').replace(/^([-\d.]+)(de.*)?$/, '$1');
    if (/^-?\d+(\.\d+)?$/.test(num)) return { t: 'n', v: parseFloat(num) };
    if (s === '' || s === '—') return { t: 'e', v: '' };
    return { t: 's', v: s };
  }

  function comparar(a, b, dir) {
    if (a.t === 'e' && b.t !== 'e') return 1;          // los vacíos siempre al final
    if (b.t === 'e' && a.t !== 'e') return -1;
    var r = (a.t === 'n' && b.t === 'n') ? a.v - b.v : coll.compare(String(a.v), String(b.v));
    return dir === 'ascending' ? r : -r;
  }

  function activar(tabla) {
    var cab = tabla.tHead && tabla.tHead.rows[tabla.tHead.rows.length - 1];
    var cuerpo = tabla.tBodies[0];
    if (!cab || !cuerpo || cuerpo.rows.length < 2) return;
    for (var i = 0; i < cuerpo.rows.length; i++) {       // filas con celdas combinadas = mensaje, no datos
      for (var j = 0; j < cuerpo.rows[i].cells.length; j++) if (cuerpo.rows[i].cells[j].colSpan > 1) return;
    }
    tabla.classList.add('ordenable');
    Array.prototype.forEach.call(cab.cells, function (th, idx) {
      if (!th.textContent.trim()) return;
      th.classList.add('ordenable');
      th.tabIndex = 0;
      th.setAttribute('role', 'columnheader');
      var ordenar = function () {
        var dir = th.getAttribute('aria-sort') === 'descending' ? 'ascending' : 'descending';
        // primer clic: de mayor a menor (lo más habitual al auditar); segundo: de menor a mayor
        Array.prototype.forEach.call(cab.cells, function (x) { x.removeAttribute('aria-sort'); });
        th.setAttribute('aria-sort', dir);
        var filas = Array.prototype.slice.call(cuerpo.rows).map(function (tr, orden) {
          return { tr: tr, v: valor(tr.cells[idx]), o: orden };
        });
        filas.sort(function (a, b) { return comparar(a.v, b.v, dir) || a.o - b.o; });
        filas.forEach(function (f) { cuerpo.appendChild(f.tr); });
      };
      th.addEventListener('click', ordenar);
      th.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ordenar(); } });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(document.querySelectorAll('table:not(.no-sort):not(.matrix)'), activar);
  });
})();
