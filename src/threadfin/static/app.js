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

  // XMLTV channel picker (mapping dialog): a chosen guide channel activates the channel,
  // "None" deactivates it (as in Go). Choosing the dummy guide stops logo updates from the playlist.
  function syncActive(picker) {
    const form = picker.closest("form");
    const mapping = picker.querySelector("input[name='x-mapping']");
    const active = form && form.querySelector("input[name='x-active']");
    if (active && mapping) active.checked = mapping.value !== "" && mapping.value !== "-";
  }

  document.addEventListener("click", (event) => {
    const choice = event.target.closest("[data-xmltv-id]");
    const picker = choice && choice.closest(".xmltv-picker");
    if (!picker) return;
    picker.querySelector("input[name='x-mapping']").value = choice.dataset.xmltvId;
    const search = picker.querySelector("input[type=search]");
    search.value = picker.dataset.label = choice.dataset.label;
    picker.querySelector(".matches").innerHTML = "";
    syncActive(picker);
    search.focus();
  });

  // Leaving the search box without picking shows the current choice again, not the typed text.
  document.addEventListener("focusout", (event) => {
    const picker = event.target.closest && event.target.closest(".xmltv-picker");
    if (!picker || picker.contains(event.relatedTarget)) return;
    picker.querySelector("input[type=search]").value = picker.dataset.label || "";
    picker.querySelector(".matches").innerHTML = "";
  });

  // A new picker after another guide file was chosen. (htmx:load fires on the inserted element
  // itself; when the dialog opens it fires on the form, so a saved Active state is left alone.)
  document.addEventListener("htmx:load", (event) => {
    const picker = event.target;
    if (!(picker instanceof HTMLElement) || picker.id !== "xmltv-picker") return;
    syncActive(picker);
    if (picker.dataset.dummy === "true") {
      const icon = picker.closest("form").querySelector("input[name='x-update-channel-icon']");
      if (icon) icon.checked = false;
    }
  });

  // Select all / none in the mapping table.
  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-select-all]")) {
      document.querySelectorAll("input[name=ids]").forEach((box) => { box.checked = event.target.checked; });
    }
  });
})();
