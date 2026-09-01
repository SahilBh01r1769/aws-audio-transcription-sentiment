"use strict";

const replacements = [
  [/—/g, "-"],
  [/⚠\s*/g, "Warning: "],
  [/✓/g, ""],
  [/🎙|📁|📜|✨|⭐|🌟/g, ""],
];

function sanitizeTextNode(node) {
  if (!node || node.nodeType !== Node.TEXT_NODE) return;
  let next = node.nodeValue || "";
  for (const [pattern, replacement] of replacements) next = next.replace(pattern, replacement);
  if (next !== node.nodeValue) node.nodeValue = next;
}

function sanitizeElement(element) {
  if (!element || element.nodeType !== Node.ELEMENT_NODE) return;
  const inline = element.getAttribute("style") || "";
  if (inline.toLowerCase().includes("#c084fc")) {
    element.style.color = "var(--mixed)";
  }
}

function refreshLoadingState(root = document) {
  root.querySelectorAll?.("#log-body td").forEach(cell => {
    const loading = (cell.textContent || "").trim().toLowerCase().startsWith("loading");
    cell.classList.toggle("skeleton-cell", loading);
  });
}

function sanitizeTree(root) {
  if (root.nodeType === Node.TEXT_NODE) {
    sanitizeTextNode(root);
    return;
  }
  sanitizeElement(root);
  root.querySelectorAll?.("[style]").forEach(sanitizeElement);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) sanitizeTextNode(node);
  refreshLoadingState(root.nodeType === Node.ELEMENT_NODE ? root : document);
}

sanitizeTree(document.body);

const observer = new MutationObserver(mutations => {
  for (const mutation of mutations) {
    if (mutation.type === "characterData") sanitizeTextNode(mutation.target);
    if (mutation.type === "attributes") sanitizeElement(mutation.target);
    mutation.addedNodes.forEach(sanitizeTree);
  }
  refreshLoadingState();
});

observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["style"] });
