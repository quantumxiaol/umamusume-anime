import { Audio } from "@remotion/media";
import { AbsoluteFill, Sequence, staticFile, useVideoConfig } from "remotion";
import { z } from "zod";
import { DEFAULT_FPS, introDurationFrames } from "../lib/constants";
import { TimelineSchema } from "../lib/types";
import { calculateFrameTiming, getAudioPath } from "../lib/utils";
import { Background } from "./Background";
import Subtitle from "./Subtitle";

export const aiVideoSchema = z.object({
  contentProject: z.string(),
  timeline: TimelineSchema.nullable(),
  renderFps: z.number().optional(),
});

const titleFontFamily = "Georgia, 'Times New Roman', serif";

export const AIVideo: React.FC<z.infer<typeof aiVideoSchema>> = ({
  contentProject,
  timeline,
}) => {
  const { fps = DEFAULT_FPS } = useVideoConfig();
  if (!timeline) {
    throw new Error("Expected timeline to be fetched");
  }
  const introDuration = introDurationFrames(fps);

  return (
    <AbsoluteFill style={{ backgroundColor: "white" }}>
      <Sequence durationInFrames={introDuration}>
        <AbsoluteFill
          style={{
            justifyContent: "center",
            alignItems: "center",
            textAlign: "center",
            display: "flex",
            zIndex: 10,
          }}
        >
          <div
            style={{
              fontSize: 120,
              lineHeight: "122px",
              width: "87%",
              color: "black",
              fontFamily: titleFontFamily,
              textTransform: "uppercase",
              backgroundColor: "yellow",
              paddingTop: 20,
              paddingBottom: 20,
              border: "10px solid black",
            }}
          >
            {timeline.shortTitle}
          </div>
        </AbsoluteFill>
      </Sequence>

      {timeline.elements.map((element, index) => {
        const { startFrame, duration } = calculateFrameTiming(
          element.startMs,
          element.endMs,
          { addIntroOffset: true, fps },
        );

        return (
          <Sequence
            key={`element-${index}`}
            from={startFrame}
            durationInFrames={duration}
            premountFor={3 * fps}
          >
            <Background project={contentProject} item={element} />
          </Sequence>
        );
      })}

      {timeline.text.map((element, index) => {
        const { startFrame, duration } = calculateFrameTiming(
          element.startMs,
          element.endMs,
          { addIntroOffset: true, fps },
        );

        return (
          <Sequence
            key={`element-${index}`}
            from={startFrame}
            durationInFrames={duration}
          >
            <Subtitle item={element} durationInFrames={duration} />
          </Sequence>
        );
      })}

      {timeline.audio.map((element, index) => {
        const { startFrame, duration } = calculateFrameTiming(
          element.startMs,
          element.endMs,
          { addIntroOffset: true, fps },
        );

        return (
          <Sequence
            key={`element-${index}`}
            from={startFrame}
            durationInFrames={duration}
            premountFor={3 * fps}
          >
            <Audio
              src={staticFile(getAudioPath(contentProject, element.audioUrl))}
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
