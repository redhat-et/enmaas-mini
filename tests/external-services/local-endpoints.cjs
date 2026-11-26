// Keep the browser origins consistent with local.py. No arbitrary remote URLs.
const raw = process.env.ENMAAS_LAB_PORT_OFFSET ?? '0';
if (!/^\d+$/.test(raw) || Number(raw) > 30000) {
  throw new Error('Invalid local lab port offset');
}
const offset = Number(raw);
const ports = { issuer: 18443, observability: 19443, collector: 24443, gateway: 28443 };
function port(role) {
  if (!Object.hasOwn(ports, role)) throw new Error('Unknown local service');
  return ports[role] + offset;
}
function url(role, path = '') { return `https://localhost:${port(role)}${path}`; }
module.exports = { port, url };
