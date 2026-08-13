document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector("[data-menu-toggle]");
  const nav = document.querySelector("[data-nav]");
  toggle?.addEventListener("click", () => nav?.classList.toggle("open"));

  document.querySelectorAll("[data-dismiss]").forEach((button) => {
    button.addEventListener("click", () => button.parentElement?.remove());
  });

  const input = document.querySelector("[data-file-input]");
  const name = document.querySelector("[data-file-name]");
  input?.addEventListener("change", () => {
    const file = input.files?.[0];
    if (name) name.textContent = file ? file.name : "No image selected";
  });

  window.setTimeout(() => {
    document.querySelectorAll(".flash").forEach((flash) => flash.remove());
  }, 5500);
});