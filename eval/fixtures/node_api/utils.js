// Small utility library for eval/ refactor, feature, and dependency tasks.
function slugify(s) {
  return String(s).toLowerCase().trim().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
}

function formatPrice(cents) {
  return '$' + (cents / 100).toFixed(2);
}

function chunk(arr, size) {
  const out = [];
  for (let i = 0; i < arr.length; i += size) {
    out.push(arr.slice(i, i + size));
  }
  return out;
}

module.exports = { slugify, formatPrice, chunk };
