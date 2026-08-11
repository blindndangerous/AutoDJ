import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import viteConfig from "../../vite.config.js";

const ESBUILD_VERSION = "0.28.2";
const REGISTRY_PREFIX = "https://registry.npmjs.org/esbuild/-/esbuild-";
const ESBUILD_INTEGRITY =
  "sha512-HKVLS8dvII+xoKW9kmqxbRKrnWEXfJJr/FZhhJmiqIB0e053QNYFqOBouTMO/k5sID4MvCiUCvv8b9M4h32wIA==";
const ESBUILD_PLATFORM_FIXTURE = [
  ["@esbuild/aix-ppc64", "aix", "ppc64",
    "sha512-XExcO+dvLKvVtNTibSTBej1NCAbaGhWn9Ww1ZPx80qsahhPFe/8jgWP0IchNe0F3HwkU7n8ejhH8bjonqht8mQ=="],
  ["@esbuild/android-arm", "android", "arm",
    "sha512-kXXoiPVVGQcnIYGOeaovwOURpniDBpSq4A03qkQ+BMQqtGG6HYap3xne9C1O1yo4TR3qxlCX5IqqmX6fFo2Lqg=="],
  ["@esbuild/android-arm64", "android", "arm64",
    "sha512-5YfKeeI8qWfBZIX+u2xZC3Zlb3Os/gLS2sbEKM+I4ZOcsWmHS2WLysCcQZDAFRslDUU5Oiq44gf6PYN1vGwG5A=="],
  ["@esbuild/android-x64", "android", "x64",
    "sha512-O387ite7SzUyCcy3JQX4P4bLtEA7bLLkx+esve5JHnyYfNTxcVpXZo9jhdB0lTKN44gztELTdU7nS8Nr16Fs1Q=="],
  ["@esbuild/darwin-arm64", "darwin", "arm64",
    "sha512-n4KqkOQrraxHJcgjM1RvwbigfQKIKJVpM7xp+KsxiyUSrRdIXnt73VhrPAx0fV44hgfmIVKjxMN9J1t5jySVkw=="],
  ["@esbuild/darwin-x64", "darwin", "x64",
    "sha512-uq6suIWYP37qzGddBKPw5QEQPi6HiLGsO7UmkpfyaYNQ3D+rN6w6WfwH+nuqcGXWvawGwxOEroO4YGnFh95azw=="],
  ["@esbuild/freebsd-arm64", "freebsd", "arm64",
    "sha512-n+I0BTSRIoy+d6RPKnEVwql5UwBJolytvY4mAOIEJorKlqgPII8ix6slVVrfZ5Tnj7glIZvloylbB/EJPMWEXw=="],
  ["@esbuild/freebsd-x64", "freebsd", "x64",
    "sha512-78XJTJkvPs0kz2w61301PJjXl4g7q3JqiYMZ/M/yVI73EHBrCRTgkhu9oqG7vPqq+a/yadEW8aD+agKlk5xrmg=="],
  ["@esbuild/linux-arm", "linux", "arm",
    "sha512-XlDnu2q5yoqems+xay6wSAcg9DDD7K9RLKZEBOMZm3ckNpJBvOX20tSfby8KfrrhINDyv9V2YVZKY/SpoGJI8w=="],
  ["@esbuild/linux-arm64", "linux", "arm64",
    "sha512-pW4AC0P3it8c7do9MVM4p51FzHzdM/TZrerurgRcHJ2WTa1VQ1CIq18xncfpBJw4ojkiZZrKW2yIBWBP92j6Ug=="],
  ["@esbuild/linux-ia32", "linux", "ia32",
    "sha512-CYbnj78HsIeA+DhgUKgFCfvNsTHFhMMrinUrMZpDXJXKN8T3XViTZ/+wtHeVxEWY8ewSzTFN+nRmSwO2tZaLUQ=="],
  ["@esbuild/linux-loong64", "linux", "loong64",
    "sha512-buwkd8nsph4R+ajRvw0qM5Hja/TXQow3ptzWO2EbG/cqcIkHloRrdlBtQlshyYGTNFvfkfJ5tpPLVkY4DtsPfQ=="],
  ["@esbuild/linux-mips64el", "linux", "mips64el",
    "sha512-ZVykbDyk7519VwiNb9Lcj9m8XM6v5V9uKPvrEMkkEedVewf+0itkhahp4HDpgERXhwLRpWFypsGbG/J8s0QjJA=="],
  ["@esbuild/linux-ppc64", "linux", "ppc64",
    "sha512-CAXl+Dtd9UUuJd8pKKdwh6MLm3MUMiqMPmhZ3tTSXPqfyQ3vDl6R5hZdZ/kYojK4ofXtdfSv1tFq8XzWx3heNQ=="],
  ["@esbuild/linux-riscv64", "linux", "riscv64",
    "sha512-GeXCej4IQtU1B+QlDV8W/RRvbzI3O/Stss+/bCXv4lZls5WGRtu2a+3JkA3i4qIUlMXpcHebWpF8AkJhATowuA=="],
  ["@esbuild/linux-s390x", "linux", "s390x",
    "sha512-3H1weTYZPxt/WOhByszQZybS9w5lKzUn1FDMsgEChbHWQwHYQQRfBxgCcZvPhjHfKyJjIievvMmEUawJrdY9Dg=="],
  ["@esbuild/linux-x64", "linux", "x64",
    "sha512-4xTZr1FUmSoQW4XIWmit3tzQrUTZM+N3P0XV8xROKYF50XfI7xeO90+1bZvNwxIufQ9hDQVRJH5YhgPVF8A/HQ=="],
  ["@esbuild/netbsd-arm64", "netbsd", "arm64",
    "sha512-sSATRjPeDBg3pdgHoQfoYBob11Kk1FGa9lui5RIHZCoCkJa9QKlvl3/vKz2usCmYYjs7ymJR/2Nnsqe+Hjt5nw=="],
  ["@esbuild/netbsd-x64", "netbsd", "x64",
    "sha512-lqnzCV+mM0gIADaKihiCg6ifgfU2L3h5E33rNQBN1Y4MaVGnzryzmvvf7UHxprpQdE8hpqLolJ9Rl+SkIRDpyw=="],
  ["@esbuild/openbsd-arm64", "openbsd", "arm64",
    "sha512-AL2qJILH7lNjrDmCQDvdxMfAUIv8KMNZOvrwAQ8i8//ntL9FflhOyMJ8OZSMBb8/AWXe3/5v5S20y3zCoZWKoQ=="],
  ["@esbuild/openbsd-x64", "openbsd", "x64",
    "sha512-QtiuPytchRyC4rwUKhexJdQKvDuZ6hWloi3igqPQNUJCS1/v9EiO3UTOXR6A3FoMo4fnAKbWJdqaIwhOzh8qEw=="],
  ["@esbuild/openharmony-arm64", "openharmony", "arm64",
    "sha512-WkhYDmpTjLvGlScA1rwjRUmhl4k8oXR3cIbtqWmELgU/dFeHHlEllxDvdWcNJV9rbzCexB5vz8gtNewWLgCT7Q=="],
  ["@esbuild/sunos-x64", "sunos", "x64",
    "sha512-GPMSkTOtMnv2U2F8gxe4Io6qmVs+YKyp832Etqqxr0hFngmXQ3rzwytelm3GIn7T4VviRUlf3sOgBOiTdvaf7g=="],
  ["@esbuild/win32-arm64", "win32", "arm64",
    "sha512-PIhhEkE9uPBleRBrQEJpUn7MBnibZzbGzYWPmY3x+YoVg/95zbjB4CxPPOQ8l5tYYM4mMaCthF8/1DIfBQQyWQ=="],
  ["@esbuild/win32-ia32", "win32", "ia32",
    "sha512-YmJbfTlvU7Sdn9BB+4PRES4oB6pxgS37MAONj+hBr/cpXS1aBPKXxNnDbu+QCWPj0o9dgyxeq79g6c5P8KeuYA=="],
  ["@esbuild/win32-x64", "win32", "x64",
    "sha512-5ebpxr3nWMzrL/rnUI755Jkuee0bHL/Gq0WTF9lvcpv73wAp5eu8MfBUgWK9bhWvZjj7yX8etf/8tI8Ney695g=="],
];

const EXPECTED_ESBUILD_PLATFORMS = Object.fromEntries(
  ESBUILD_PLATFORM_FIXTURE.map(([packageName, os, cpu, integrity]) => {
    const archiveName = packageName.slice("@esbuild/".length);
    return [packageName, {
      version: ESBUILD_VERSION,
      os: [os],
      cpu: [cpu],
      tarball: `https://registry.npmjs.org/${packageName}/-/${archiveName}-${ESBUILD_VERSION}.tgz`,
      integrity,
      optional: true,
    }];
  }),
);

function readJson(path) {
  return JSON.parse(readFileSync(join(process.cwd(), path), "utf8"));
}

function zeroMajorCaretRangeIncludes(range, version) {
  const [, minorText, patchText] = /^0\.(\d+)\.(\d+)$/.exec(version) ?? [];
  if (minorText === undefined) return false;

  const minor = Number(minorText);
  const patch = Number(patchText);
  return range.split("||").some(part => {
    const match = /^\s*\^0\.(\d+)\.(\d+)\s*$/.exec(part);
    return match !== null && Number(match[1]) === minor && patch >= Number(match[2]);
  });
}

describe("Vite esbuild build dependency contract", () => {
  it("pins the configured esbuild minifier directly at an exact compatible locked artifact", () => {
    const manifest = readJson("package.json");
    const lock = readJson("package-lock.json");
    const viteManifest = readJson("node_modules/vite/package.json");
    const esbuildManifest = readJson("node_modules/esbuild/package.json");
    const lockedEsbuild = lock.packages["node_modules/esbuild"];

    expect(viteConfig.build.minify).toBe("esbuild");
    expect(manifest.devDependencies.esbuild).toBe(ESBUILD_VERSION);
    expect(manifest.devDependencies.esbuild).toMatch(/^\d+\.\d+\.\d+$/);
    expect(zeroMajorCaretRangeIncludes(
      viteManifest.peerDependencies.esbuild,
      ESBUILD_VERSION,
    )).toBe(true);
    expect(lock.packages[""].devDependencies.esbuild).toBe(ESBUILD_VERSION);
    expect(lockedEsbuild).toMatchObject({
      version: ESBUILD_VERSION,
      resolved: `${REGISTRY_PREFIX}${ESBUILD_VERSION}.tgz`,
      integrity: ESBUILD_INTEGRITY,
    });

    const expectedOptionalDependencies = Object.fromEntries(
      Object.entries(EXPECTED_ESBUILD_PLATFORMS)
        .map(([packageName, metadata]) => [packageName, metadata.version]),
    );
    expect(esbuildManifest.version).toBe(ESBUILD_VERSION);
    expect(esbuildManifest.optionalDependencies).toEqual(expectedOptionalDependencies);
    expect(lockedEsbuild.optionalDependencies).toEqual(expectedOptionalDependencies);

    const lockedPlatformNames = Object.keys(lock.packages)
      .filter(packagePath => packagePath.startsWith("node_modules/@esbuild/"))
      .map(packagePath => packagePath.slice("node_modules/".length))
      .sort();
    expect(lockedPlatformNames).toEqual(Object.keys(EXPECTED_ESBUILD_PLATFORMS).sort());

    const lockedPlatformMetadata = Object.fromEntries(
      lockedPlatformNames.map(packageName => {
        const artifact = lock.packages[`node_modules/${packageName}`];
        return [packageName, {
          version: artifact.version,
          os: artifact.os,
          cpu: artifact.cpu,
          tarball: artifact.resolved,
          integrity: artifact.integrity,
          optional: artifact.optional,
        }];
      }),
    );
    expect(lockedPlatformMetadata).toEqual(EXPECTED_ESBUILD_PLATFORMS);
  });
});
