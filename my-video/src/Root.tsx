import "./index.css";
import { Composition, getStaticFiles } from "remotion";
import { AIVideo, aiVideoSchema } from "./components/AIVideo";
import { DEFAULT_FPS, introDurationFrames } from "./lib/constants";
import { getTimelinePath, loadTimelineFromFile } from "./lib/utils";

const resolveRenderFps = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) && value > 0
    ? value
    : DEFAULT_FPS;

export const RemotionRoot: React.FC = () => {
  const staticFiles = getStaticFiles();
  const timelines = staticFiles
    .filter((file) => file.name.endsWith("timeline.json"))
    .map((file) => file.name.split("/")[1]);

  const toCompositionId = (storyName: string) =>
    storyName.replace(/[^a-zA-Z0-9\u4e00-\u9fff-]/g, "-");

  return (
    <>
      {timelines.map((storyName) => (
        <Composition
          id={toCompositionId(storyName)}
          component={AIVideo}
          fps={DEFAULT_FPS}
          width={1920}
          height={1080}
          schema={aiVideoSchema}
          defaultProps={{
            contentProject: storyName,
            timeline: null,
            renderFps: DEFAULT_FPS,
          }}
          calculateMetadata={async ({ props }) => {
            const fps = resolveRenderFps(props.renderFps);
            const { lengthFrames, timeline } = await loadTimelineFromFile(
              getTimelinePath(storyName),
              fps,
            );

            return {
              durationInFrames: lengthFrames + introDurationFrames(fps),
              fps,
              width: timeline.width ?? 1920,
              height: timeline.height ?? 1080,
              props: {
                ...props,
                timeline,
              },
            };
          }}
        />
      ))}
    </>
  );
};
