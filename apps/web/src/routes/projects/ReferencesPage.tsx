import { MessageBar, MessageBarBody, Title1, makeStyles, tokens } from "@fluentui/react-components";
import { useQuery } from "@tanstack/react-query";
import type { JSX } from "react";
import { getReferences } from "../../api/references";
import { useApiClient } from "../../app/apiContext";
import { CharacterReferencePanel } from "../../components/CharacterReferencePanel";
import { ContinuityStatePanel } from "../../components/ContinuityStatePanel";
import { LocationReferencePanel } from "../../components/LocationReferencePanel";
import { ErrorState, LoadingState } from "../../components/states";
import { useProjectContext } from "./useProjectContext";

const useStyles = makeStyles({ grid: { display: "grid", gap: tokens.spacingVerticalL } });

export function ReferencesPage(): JSX.Element {
  const styles = useStyles();
  const client = useApiClient();
  const { projectId } = useProjectContext();
  const query = useQuery({
    queryKey: ["projects", projectId, "references"],
    queryFn: ({ signal }) => getReferences(projectId, client, signal).then((value) => value.data),
    enabled: projectId !== "",
  });
  if (query.isLoading) return <LoadingState label="Loading continuity references" />;
  if (query.isError || !query.data) return <ErrorState title="References unavailable" error={query.error} onRetry={() => void query.refetch()} />;
  return <section className={styles.grid} aria-labelledby="references-heading">
    <Title1 id="references-heading">Character and location references</Title1>
    <MessageBar><MessageBarBody>Only approved immutable versions are injected into production keyframes. Review downstream invalidation before approval.</MessageBarBody></MessageBar>
    <CharacterReferencePanel versions={query.data.characters} />
    <LocationReferencePanel versions={query.data.locations} />
    <ContinuityStatePanel bindings={query.data.bindings} />
  </section>;
}
