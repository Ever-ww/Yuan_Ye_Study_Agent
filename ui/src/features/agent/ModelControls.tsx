import type { ModelOption, ReasoningEffort } from "../../types";

const allEfforts: ReasoningEffort[] = ["none", "low", "medium", "high", "xhigh", "max"];

export function ModelControls({
  models, profileId, effort, disabled, onProfile, onEffort,
}: {
  models: ModelOption[];
  profileId: string;
  effort: ReasoningEffort;
  disabled: boolean;
  onProfile: (value: string) => void;
  onEffort: (value: ReasoningEffort) => void;
}) {
  const model = models.find((item) => item.profile_id === profileId);
  const supported = model?.supported_reasoning_efforts?.length ? model.supported_reasoning_efforts : allEfforts;
  return (
    <>
      <label className="sr-only" htmlFor="model-profile">模型</label>
      <select id="model-profile" value={profileId} disabled={disabled} onChange={(event) => onProfile(event.target.value)}>
        {models.map((item) => <option value={item.profile_id} key={item.profile_id}>{item.model}</option>)}
      </select>
      <span className="control-divider" aria-hidden="true" />
      <label className="sr-only" htmlFor="reasoning-effort">思考强度</label>
      <select id="reasoning-effort" value={effort} disabled={disabled} onChange={(event) => onEffort(event.target.value as ReasoningEffort)}>
        {supported.map((item) => <option value={item} key={item}>{item}</option>)}
      </select>
    </>
  );
}
