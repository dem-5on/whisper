export function hasFeature(features, feature) {
  return Boolean(features && typeof features === "object" && features[feature] === true);
}

export function isCloudBackend(backend) {
  return typeof backend === "string" && backend !== "local";
}
