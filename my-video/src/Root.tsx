import "./index.css";
import { Composition, getStaticFiles } from "remotion";
import { AIVideo, aiVideoSchema } from "./components/AIVideo";
import { FPS, INTRO_DURATION } from "./lib/constants";
import { getTimelinePath, loadTimelineFromFile } from "./lib/utils";

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
          fps={FPS}
          width={1920}
          height={1080}
          schema={aiVideoSchema}
          defaultProps={{
            contentProject: storyName,
            timeline: null,
          }}
          calculateMetadata={async ({ props }) => {
            const { lengthFrames, timeline } = await loadTimelineFromFile(
              getTimelinePath(storyName),
            );

            return {
              durationInFrames: lengthFrames + INTRO_DURATION,
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
