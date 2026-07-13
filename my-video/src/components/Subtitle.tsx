import React from "react";
import {
  AbsoluteFill,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { normalizeSubtitleElement } from "../lib/subtitle-normalize";
import { getSubtitleUiScale, storySubtitleTheme } from "../lib/subtitle-theme";
import type { TextElement } from "../lib/types";
import { OutlinedText } from "./OutlinedText";

const clamp = {
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;

const Subtitle: React.FC<{
  item: TextElement;
  durationInFrames: number;
}> = ({ item, durationInFrames }) => {
  const frame = useCurrentFrame();
  const { width, height } = useVideoConfig();
  const subtitle = normalizeSubtitleElement(item);
  const uiScale = getSubtitleUiScale(width, height);
  const lastFrame = Math.max(0, Math.ceil(durationInFrames) - 1);
  const enterEnd = Math.min(storySubtitleTheme.enterFrames - 1, lastFrame);
  const exitStart = Math.max(
    enterEnd,
    lastFrame - (storySubtitleTheme.exitFrames - 1),
  );
  const dividerEnd = Math.min(
    storySubtitleTheme.dividerEnterFrames - 1,
    lastFrame,
  );
  const enterOpacity =
    enterEnd === 0 ? 1 : interpolate(frame, [0, enterEnd], [0, 1], clamp);
  const exitOpacity =
    exitStart === lastFrame
      ? 1
      : interpolate(frame, [exitStart, lastFrame], [1, 0], clamp);
  const opacity = Math.min(enterOpacity, exitOpacity);
  const translateY =
    enterEnd === 0
      ? 0
      : interpolate(
          frame,
          [0, enterEnd],
          [storySubtitleTheme.enterOffsetY * uiScale, 0],
          clamp,
        );
  const dividerProgress =
    dividerEnd === 0 ? 1 : interpolate(frame, [0, dividerEnd], [0, 1], clamp);
  const hasSpeaker =
    subtitle.kind !== "narration" && Boolean(subtitle.speakerLabel);
  const hasJa = Boolean(subtitle.subtitleJa);
  const hasZh = Boolean(subtitle.subtitleZh);

  if (!hasJa && !hasZh) {
    return null;
  }

  const bodyTextStyle = {
    whiteSpace: "pre-wrap",
    textAlign: "left",
    wordBreak: "normal",
    lineBreak: "strict",
    overflowWrap: "break-word",
  } as const;

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <AbsoluteFill
        style={{
          background:
            "linear-gradient(to bottom, rgba(0, 0, 0, 0) 58%, rgba(0, 0, 0, 0.04) 74%, rgba(0, 0, 0, 0.16) 100%)",
          opacity,
        }}
      />

      <div
        style={{
          position: "absolute",
          left: width * storySubtitleTheme.leftRatio,
          width: width * storySubtitleTheme.widthRatio,
          bottom: storySubtitleTheme.bottom * uiScale,
          opacity,
          transform: `translateY(${translateY}px)`,
        }}
      >
        <div>
          {hasSpeaker ? (
            <OutlinedText
              fontSize={storySubtitleTheme.speakerFontSize * uiScale}
              fontWeight={storySubtitleTheme.speakerWeight}
              lineHeight={1.15}
              uiScale={uiScale}
              style={{
                marginBottom: 4 * uiScale,
                textAlign: "center",
                width: `${storySubtitleTheme.speakerAnchorWidthRatio * 100}%`,
              }}
            >
              {subtitle.speakerLabel}
            </OutlinedText>
          ) : null}
          <div
            style={{
              width: "100%",
              height: storySubtitleTheme.dividerHeight * uiScale,
              backgroundColor: "rgba(255, 255, 255, 0.96)",
              boxShadow: `0 ${1 * uiScale}px ${3 * uiScale}px rgba(0, 0, 0, 0.72)`,
              transform: `scaleX(${dividerProgress})`,
              transformOrigin: "left center",
            }}
          />
        </div>

        <div
          style={{
            marginLeft: storySubtitleTheme.bodyIndent * uiScale,
            marginRight: storySubtitleTheme.bodyIndent * uiScale,
            marginTop: storySubtitleTheme.dividerGap * uiScale,
          }}
        >
          {hasJa ? (
            <OutlinedText
              fontSize={storySubtitleTheme.bodyFontSize * uiScale}
              fontWeight={storySubtitleTheme.bodyWeight}
              lineHeight={storySubtitleTheme.bodyLineHeight}
              uiScale={uiScale}
              style={bodyTextStyle}
            >
              {subtitle.subtitleJa}
            </OutlinedText>
          ) : null}

          {hasZh ? (
            <OutlinedText
              color="#ffffff"
              fontSize={storySubtitleTheme.bodyFontSize * uiScale}
              fontWeight={storySubtitleTheme.bodyWeight}
              lineHeight={storySubtitleTheme.bodyLineHeight}
              uiScale={uiScale}
              style={{
                ...bodyTextStyle,
                marginTop: hasJa ? storySubtitleTheme.languageGap * uiScale : 0,
              }}
            >
              {subtitle.subtitleZh}
            </OutlinedText>
          ) : null}
        </div>
      </div>
    </AbsoluteFill>
  );
};

export default Subtitle;
