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
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) sanitizeTextNode(node);
  refreshLoadingState(root.nodeType === Node.ELEMENT_NODE ? root : document);
}

sanitizeTree(document.body);

const observer = new MutationObserver(mutations => {
  for (const mutation of mutations) {
    if (mutation.type === "characterData") sanitizeTextNode(mutation.target);
    mutation.addedNodes.forEach(sanitizeTree);
  }
  refreshLoadingState();
});

observer.observe(document.body, { childList: true, subtree: true, characterData: true });
