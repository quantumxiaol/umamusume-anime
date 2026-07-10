import type { CSSProperties, ReactNode } from "react";
import { subtitleFontFamily } from "../lib/subtitle-theme";

export const OutlinedText: React.FC<{
  children: ReactNode;
  fontSize: number;
  uiScale: number;
  color?: string;
  fontWeight?: CSSProperties["fontWeight"];
  lineHeight?: CSSProperties["lineHeight"];
  style?: CSSProperties;
}> = ({
  children,
  fontSize,
  uiScale,
  color = "#ffffff",
  fontWeight = 800,
  lineHeight = 1.25,
  style,
}) => {
  return (
    <div
      style={{
        color,
        fontFamily: subtitleFontFamily,
        fontSize,
        fontWeight,
        lineHeight,
        WebkitTextStroke: `${2.2 * uiScale}px rgba(10, 14, 18, 0.82)`,
        paintOrder: "stroke fill",
        textShadow: `0 ${2 * uiScale}px ${2 * uiScale}px rgba(0, 0, 0, 0.85), 0 0 ${6 * uiScale}px rgba(0, 0, 0, 0.45)`,
        ...style,
      }}
    >
      {children}
    </div>
  );
};
