import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

const REGISTRY_PREFIX = "https://registry.npmjs.org/";
const SUBRESOURCE_INTEGRITY = /^sha(?:256|384|512)-\S+$/;

function metadataException(packagePath, metadata) {
  if (packagePath === "") return "lock root";
  if (metadata.link === true) return "link";
  if (metadata.inBundle === true || metadata.bundled === true) return "bundled";
  return null;
}

export function packageLockIntegrityErrors(lock) {
  if (!lock || typeof lock !== "object" || !lock.packages
    || typeof lock.packages !== "object") {
    return ["package-lock.json must contain a packages object"];
  }

  const errors = [];
  for (const [packagePath, metadata] of Object.entries(lock.packages)) {
    if (metadataException(packagePath, metadata)) continue;
    const missing = [];
    if (typeof metadata.resolved !== "string"
      || !metadata.resolved.startsWith(REGISTRY_PREFIX)) {
      missing.push("resolved registry URL");
    }
    if (typeof metadata.integrity !== "string"
      || !SUBRESOURCE_INTEGRITY.test(metadata.integrity)) {
      missing.push("integrity");
    }
    if (missing.length) errors.push(`${packagePath}: missing ${missing.join(" and ")}`);
  }
  return errors;
}

function main() {
  const lockPath = process.argv[2] || "package-lock.json";
  let lock;
  try {
    lock = JSON.parse(readFileSync(lockPath, "utf8"));
  } catch (error) {
    console.error(`Unable to read ${lockPath}: ${error.message}`);
    return 1;
  }

  const errors = packageLockIntegrityErrors(lock);
  if (errors.length) {
    console.error("Package-lock registry integrity check failed:");
    for (const error of errors) console.error(`- ${error}`);
    console.error("Regenerate package-lock.json from an empty install directory.");
    return 1;
  }
  console.log("Package-lock registry integrity check passed.");
  return 0;
}

if (process.argv[1]
  && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = main();
}
