import { configure } from "@testing-library/dom";
import { beforeEach } from "vitest";
import { setExpertView } from "../lib/expert";

/**
 * The suite renders whole screens (routing, redirects, lazy tabs) in parallel jsdom workers; on a
 * loaded machine the first render can take longer than the 1 s default of findBy/waitFor. A longer
 * ceiling changes no assertion, only how long a correct screen may take to appear.
 */
configure({ asyncUtilTimeout: 4000 });

/**
 * The app opens in business view; most suites exercise the expert panels (SQL console, notebooks,
 * builds, lineage), so they start with expert tools on. Business-view tests turn it off themselves.
 */
beforeEach(() => setExpertView(true));
