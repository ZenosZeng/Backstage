// Apply the saved preference before loading styles to avoid a theme flash.
const savedTheme = localStorage.getItem('backstage.theme');
document.documentElement.dataset.theme = ['light', 'dark'].includes(savedTheme)
  ? savedTheme
  : (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
