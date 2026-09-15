// Rotating agency slides on the overview.
// Cycles .rot-slide every data-interval ms; prev/next buttons and dots jump;
// pauses while the mouse is over the section. That is all it does.
(function () {
  var root = document.getElementById("rotator");
  if (!root) return;
  var slides = root.querySelectorAll(".rot-slide");
  var titles = root.querySelectorAll(".rot-title");
  var dots = root.querySelectorAll(".rot-dot");
  var bar = root.querySelector(".rot-progress span");
  var interval = parseInt(root.dataset.interval, 10) || 12000;
  var n = slides.length, current = 0, timer = null, paused = false;
  if (n < 2) { if (bar) bar.style.display = "none"; return; }

  function show(i) {
    current = (i + n) % n;
    slides.forEach(function (el, k) { el.classList.toggle("is-active", k === current); });
    titles.forEach(function (el, k) { el.classList.toggle("is-active", k === current); });
    dots.forEach(function (el, k) { el.classList.toggle("is-active", k === current); });
    restartBar();
  }
  function restartBar() {
    if (!bar) return;
    bar.style.transition = "none"; bar.style.width = "0%";
    void bar.offsetWidth;                                   // force reflow so the reset applies
    bar.style.transition = "width " + interval + "ms linear"; bar.style.width = "100%";
  }
  function start() { stop(); timer = setInterval(function () { if (!paused) show(current + 1); }, interval); }
  function stop() { if (timer) clearInterval(timer); }

  root.addEventListener("click", function (e) {
    var b = e.target.closest("[data-rot], .rot-dot"); if (!b) return;
    if (b.dataset.rot === "prev") show(current - 1);
    else if (b.dataset.rot === "next") show(current + 1);
    else show(parseInt(b.dataset.slide, 10));
    start();
  });
  root.addEventListener("mouseenter", function () { paused = true; });
  root.addEventListener("mouseleave", function () { paused = false; });
  show(0); start();
})();
