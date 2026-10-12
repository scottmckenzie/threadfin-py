// Glue between htmx and the page: the shared <dialog>, toasts, and server events
// (HX-Trigger: close-dialog, refresh-list, toast).
(function () {
  "use strict";
  const dialog = () => document.getElementById("dialog");

  document.addEventListener("htmx:afterSettle", (event) => {
    if (event.detail.target.id === "dialog-body" && event.detail.target.innerHTML.trim()) {
      const d = dialog();
      if (!d.open) d.showModal();
    }
  });

  document.body.addEventListener("close-dialog", () => {
    const d = dialog();
    if (d && d.open) d.close();
  });

  document.addEventListener("click", (event) => {
    if (event.target.matches("[data-close-dialog]")) dialog().close();
  });

  let timer;
  function toast(message) {
    const box = document.getElementById("toast");
    if (!box) return;
    box.textContent = message;
    box.classList.add("show");
    clearTimeout(timer);
    timer = setTimeout(() => box.classList.remove("show"), 4000);
  }
  document.body.addEventListener("toast", (event) => toast(event.detail.value));

  document.addEventListener("htmx:responseError", (event) => {
    const xhr = event.detail.xhr;
    toast(`Request failed (${xhr.status}): ${xhr.responseText.slice(0, 200)}`);
  });

  // Select all / none in the mapping table.
  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-select-all]")) {
      document.querySelectorAll("input[name=ids]").forEach((box) => { box.checked = event.target.checked; });
    }
  });
})();
