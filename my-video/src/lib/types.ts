import { CharacterAlignmentResponseModel } from "@elevenlabs/elevenlabs-js/api";
import { z } from "zod";

const BackgroundTransitionTypeSchema = z.union([
  z.literal("fade"),
  z.literal("blur"),
  z.literal("none"),
]);

const TimelineElementSchema = z.object({
  startMs: z.number(),
  endMs: z.number(),
});

const ElementAnimationSchema = TimelineElementSchema.extend({
  type: z.literal("scale"),
  from: z.number(),
  to: z.number(),
});

const BackgroundElementSchema = TimelineElementSchema.extend({
  imageUrl: z.string(),
  enterTransition: BackgroundTransitionTypeSchema.optional(),
  exitTransition: BackgroundTransitionTypeSchema.optional(),
  animations: z.array(ElementAnimationSchema).optional(),
});

const SubtitleKindSchema = z.enum(["dialogue", "narration"]);

const SubtitlePositionSchema = z.enum(["top", "bottom", "center"]);

const TextElementSchema = TimelineElementSchema.extend({
  id: z.string().optional(),
  kind: SubtitleKindSchema.optional(),
  speakerId: z.string().optional(),
  speakerLabel: z.string().optional(),
  subtitleJa: z.string().optional(),
  subtitleZh: z.string().optional(),
  text: z.string().optional(),
  position: SubtitlePositionSchema.optional(),
  animations: z.array(ElementAnimationSchema).optional(),
}).refine(
  ({ text, subtitleJa, subtitleZh }) =>
    Boolean(text?.trim() || subtitleJa?.trim() || subtitleZh?.trim()),
  {
    message: "Subtitle requires text, subtitleJa, or subtitleZh",
  },
);

const AudioElementSchema = TimelineElementSchema.extend({
  audioUrl: z.string(),
});

const TimelineSchema = z.object({
  shortTitle: z.string(),
  width: z.number().optional(),
  height: z.number().optional(),
  elements: z.array(BackgroundElementSchema),
  text: z.array(TextElementSchema),
  audio: z.array(AudioElementSchema),
});

export type BackgroundTransitionType = z.infer<
  typeof BackgroundTransitionTypeSchema
>;

export type TimelineElement = z.infer<typeof TimelineElementSchema>;
export type ElementAnimation = z.infer<typeof ElementAnimationSchema>;
export type BackgroundElement = z.infer<typeof BackgroundElementSchema>;
export type SubtitleKind = z.infer<typeof SubtitleKindSchema>;
export type SubtitlePosition = z.infer<typeof SubtitlePositionSchema>;
export type TextElement = z.infer<typeof TextElementSchema>;
export type AudioElement = z.infer<typeof AudioElementSchema>;
export type Timeline = z.infer<typeof TimelineSchema>;

export {
  AudioElementSchema,
  BackgroundElementSchema,
  BackgroundTransitionTypeSchema,
  ElementAnimationSchema,
  SubtitleKindSchema,
  SubtitlePositionSchema,
  TextElementSchema,
  TimelineElementSchema,
  TimelineSchema,
};

export const StoryScript = z.object({
  text: z.string(),
});

export const StoryWithImages = z.object({
  result: z.array(
    z.object({
      text: z.string(),
      imageDescription: z.string(),
    }),
  ),
});

export const VoiceDescriptorSchema = z.object({
  id: z.string(),
  name: z.string(),
});

export type VoiceDescriptor = z.infer<typeof VoiceDescriptorSchema>;

export interface StoryMetadataWithDetails {
  shortTitle: string;
  content: ContentItemWithDetails[];
}

export interface ContentItemWithDetails {
  text: string;
  imageDescription: string;
  uid: string;
  audioTimestamps: CharacterAlignmentResponseModel;
}
