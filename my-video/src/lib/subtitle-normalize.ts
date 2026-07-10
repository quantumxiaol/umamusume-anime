import type { SubtitleKind, TextElement } from "./types";

export type NormalizedSubtitle = {
  kind: SubtitleKind;
  speakerId?: string;
  speakerLabel?: string;
  subtitleJa?: string;
  subtitleZh?: string;
};

const LEGACY_SPEAKER_PATTERN = /^([^：:\n]{1,20})[：:]\s*(.*)$/;

const clean = (value: string | undefined) => value?.trim() || undefined;

const inferKind = (
  kind: SubtitleKind | undefined,
  speakerLabel: string | undefined,
): SubtitleKind => {
  if (kind) {
    return kind;
  }

  return speakerLabel === "旁白" ? "narration" : "dialogue";
};

const normalizeStructuredSubtitle = (
  item: TextElement,
): NormalizedSubtitle | null => {
  const subtitleJa = clean(item.subtitleJa);
  const subtitleZh = clean(item.subtitleZh);

  if (!subtitleJa && !subtitleZh) {
    return null;
  }

  const speakerLabel = clean(item.speakerLabel);

  return {
    kind: inferKind(item.kind, speakerLabel),
    speakerId: clean(item.speakerId),
    speakerLabel,
    subtitleJa,
    subtitleZh,
  };
};

const normalizeLegacySubtitle = (item: TextElement): NormalizedSubtitle => {
  const legacyText = clean(item.text) ?? "";
  const [firstLine = "", ...remainingLines] = legacyText
    .replace(/\r\n?/g, "\n")
    .split("\n");
  const speakerMatch = firstLine.match(LEGACY_SPEAKER_PATTERN);
  const parsedSpeakerLabel = clean(speakerMatch?.[1]);
  const speakerLabel = clean(item.speakerLabel) ?? parsedSpeakerLabel;
  const subtitleJa = clean(speakerMatch ? speakerMatch[2] : firstLine);
  const subtitleZh = clean(remainingLines.join("\n"));

  return {
    kind: inferKind(item.kind, speakerLabel),
    speakerId: clean(item.speakerId),
    speakerLabel,
    subtitleJa,
    subtitleZh,
  };
};

export const normalizeSubtitleElement = (
  item: TextElement,
): NormalizedSubtitle =>
  normalizeStructuredSubtitle(item) ?? normalizeLegacySubtitle(item);
