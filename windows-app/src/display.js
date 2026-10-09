export function microphoneLabel(mic, sources = []) {
  if (!mic || mic === "default") return "Default";
  const source = sources.find((item) => String(item.id) === String(mic));
  return source?.name || String(mic);
}
