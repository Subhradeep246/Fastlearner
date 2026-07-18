export interface ContentManifest {
  readonly schemaVersion: 1;
  readonly packs: readonly string[];
}

export const contentManifest: ContentManifest = {
  schemaVersion: 1,
  packs: [],
};
