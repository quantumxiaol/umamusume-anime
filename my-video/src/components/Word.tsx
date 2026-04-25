import { makeTransform, scale, translateY } from "@remotion/animation-utils";
import { loadFont } from "@remotion/google-fonts/BreeSerif";
import { fitText } from "@remotion/layout-utils";
import type React from "react";
import { AbsoluteFill, interpolate, useVideoConfig } from "remotion";

export const Word: React.FC<{
  enterProgress: number;
  text: string;
  stroke: boolean;
}> = ({ enterProgress, text, stroke }) => {
  const { fontFamily } = loadFont();
  const { width, height } = useVideoConfig();
  const lines = text.split("\n").filter((line) => line.trim().length > 0);
  const desiredFontSize = height * 0.09;
  const maxTextWidth = width * 0.9;

  const fittedFontSize = Math.min(
    ...lines.map(
      (line) =>
        fitText({
          fontFamily,
          text: line,
          withinWidth: maxTextWidth,
        }).fontSize,
    ),
  );

  const fontSize = Math.min(desiredFontSize, fittedFontSize);
  const strokeWidth = Math.max(8, Math.round(fontSize * 0.14));

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        top: undefined,
        bottom: height * 0.08,
        height: height * 0.23,
      }}
    >
      <div
        style={{
          fontSize,
          color: "white",
          WebkitTextStroke: stroke ? `${strokeWidth}px black` : undefined,
          transform: makeTransform([
            scale(interpolate(enterProgress, [0, 1], [0.8, 1])),
            translateY(interpolate(enterProgress, [0, 1], [height * 0.04, 0])),
          ]),
          fontFamily,
          textAlign: "center",
          lineHeight: 1.18,
          maxWidth: maxTextWidth,
        }}
      >
        {lines.map((line, index) => (
          <div key={`${line}-${index}`}>{line}</div>
        ))}
      </div>
    </AbsoluteFill>
  );
};
