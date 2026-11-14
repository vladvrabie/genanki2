// Draws each mermaid diagram in the site's colour mode, and again when the mode changes.
// The theme's darkmode.js sets `data-bs-theme` on <html> to "light" or "dark".
const diagrams = [...document.querySelectorAll("div.mermaid")];
const sources = diagrams.map((div) => div.textContent);

function draw() {
  const dark = document.documentElement.getAttribute("data-bs-theme") === "dark";
  mermaid.initialize({ startOnLoad: false, theme: dark ? "dark" : "default" });
  diagrams.forEach((div, i) => {
    div.removeAttribute("data-processed");
    div.textContent = sources[i];
  });
  mermaid.run({ nodes: diagrams });
}

draw();
new MutationObserver(draw).observe(document.documentElement, {
  attributeFilter: ["data-bs-theme"],
});
