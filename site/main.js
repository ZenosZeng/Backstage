(() => {
  "use strict";

  const themeKey = "backstage-site-theme";
  const themeButton = document.querySelector("[data-theme-toggle]");
  const themeLabel = document.querySelector("[data-theme-label]");
  const themeMeta = document.querySelector('meta[name="theme-color"]');

  function setTheme(theme) {
    const dark = theme === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    themeButton?.setAttribute("aria-pressed", String(dark));
    themeButton?.setAttribute("aria-label", dark ? "切换浅色模式" : "切换深色模式");
    if (themeLabel) themeLabel.textContent = dark ? "浅色" : "深色";
    themeMeta?.setAttribute("content", dark ? "#14221d" : "#f6f6ef");
  }

  let savedTheme;
  try {
    savedTheme = window.localStorage.getItem(themeKey);
  } catch {
    // Storage can be disabled; the page remains usable with its light default.
  }
  setTheme(savedTheme === "dark" ? "dark" : "light");
  themeButton?.addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    setTheme(theme);
    try {
      window.localStorage.setItem(themeKey, theme);
    } catch {
      // Theme switching also works without persistence.
    }
  });

  const copyButton = document.querySelector("[data-copy-command]");
  const command = document.getElementById("start-command");
  const copyStatus = document.querySelector("[data-copy-status]");
  copyButton?.addEventListener("click", async () => {
    if (!command || !copyStatus) return;
    copyButton.disabled = true;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(command.textContent);
      copyStatus.textContent = "命令已复制。请先检查预演结果，再执行初始化。";
    } catch {
      copyStatus.textContent = "浏览器未允许复制，请手动选择上方命令。";
    } finally {
      copyButton.disabled = false;
    }
  });
})();
