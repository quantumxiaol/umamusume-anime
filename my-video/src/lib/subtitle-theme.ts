export const subtitleFontFamily =
  "'Hiragino Sans', 'Yu Gothic', 'Noto Sans CJK JP', Arial, sans-serif";

export const storySubtitleTheme = {
  baseWidth: 1920,
  baseHeight: 1080,
  leftRatio: 0.195,
  widthRatio: 0.64,
  bottom: 46,
  bodyIndent: 50,
  speakerAnchorWidthRatio: 0.28,
  speakerFontSize: 34,
  bodyFontSize: 44,
  dividerHeight: 3,
  dividerGap: 10,
  languageGap: 8,
  speakerWeight: 800,
  bodyWeight: 800,
  bodyLineHeight: 1.25,
  enterFrames: 6,
  dividerEnterFrames: 8,
  exitFrames: 4,
  enterOffsetY: 6,
} as const;

export const getSubtitleUiScale = (width: number, height: number) =>
  Math.min(
    width / storySubtitleTheme.baseWidth,
    height / storySubtitleTheme.baseHeight,
  );
