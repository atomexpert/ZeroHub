"""Contracts for the deployed Hub shell and its UI entry points."""
import json
import re
import subprocess
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
CRITICAL_IDS = set("""
    appShell sidebar mainWorkspace workspaceTabbar mobileAccessBar messagesContainer
    messagesList composerSection composerBox promptInput modelPickerBtn modelPickerMenu
    topAccessSeg mobileAccessSeg voiceBtn liveVoiceBtn sendBtn stopBtn
""".split())

# Snapshot of the IDs present in both the Hub shell and direct JS lookups.
MIN_LINKED_IDS = 898
LINKED_IDS = set("""
    accEmailInput accLabelInput accProfileIdInput accProxyCustomBox accProxyGlobalHint
    accProxyModeGroup accProxySpecInput accProxyTestBtn accProxyTestResult accessModeSelect
    accountProfileForm accountProfilesList accountStatusPill activeChatArchivedPill
    activeChatIconBtn activeChatStatePill activeChatTitle activitySectionBadges
    activitySectionChevron activitySectionCount activitySectionRunDot activitySectionTitle
    activitySectionToggleBtn activitySectionUnreadBadge addProjExtraDirBtn addProjMemNoteBtn
    adminDockerStatusLine adminGlobalProxyCard adminSettingsSection adminSharedPane adminSubTabs
    adminUsersPane advisorAutoPickChk advisorCatalogCountBadge advisorCatalogDetails
    advisorCatalogList advisorCatalogSyncMeta advisorComposerHintChk advisorEnabledChk
    advisorRecalibrateBtn advisorSettingsSubControls advisorStatusBadge advisorStrategySelect agyAccountsList
    agyAdoptLocalCliBtn agyAutoFailoverToggle agyImportProxyMode agyImportProxySpec
    agyImportProxyTest agyImportTokensBtn agyImportTokensTextarea agyNewLabelInput
    agyNewProxyCustomBox agyNewProxyModeGroup agyNewProxyTestResult agyOAuthCancelBtn
    agyOAuthCodeInput agyOAuthCopyLinkBtn agyOAuthFlowCard agyOAuthOpenLink agyOAuthStatusHint
    agyOAuthSubmitBtn agyOAuthTimer agyPoolHeaderBadge agyPoolSummaryStats agyStartOAuthBtn
    applyCustomIconBtn attachFileBtn attachmentsPreview cancelAccountProfileBtn cancelUserBtn
    cgptAccountsList cgptAdminInstallCliWrap cgptAutoFailoverToggle cgptCancelDeviceBtn
    cgptCliMissingBanner cgptCliMissingSub cgptCliVersionMeta cgptDeviceCodeBox cgptDeviceCodeVal
    cgptDeviceFlowCard cgptDeviceStatusHint cgptDeviceTimer cgptDeviceVerifyLink cgptHeaderBadge
    cgptImportAuthBtn cgptImportAuthTextarea cgptInstallCliBtn cgptNewLabelInput
    cgptNewProxyCustomBox cgptNewProxyGlobalHint cgptNewProxyModeGroup cgptNewProxySpecInput
    cgptNewProxyTestBtn cgptNewProxyTestResult cgptRefreshBtn cgptStartDeviceAuthBtn cgptStat24h
    cgptStat24hSub cgptStat5h cgptStat5hSub cgptStat7d cgptStat7dSub cgptStatTotal cgptStatTotalSub
    cgptUpdatedTime chatActiveProcessesBar chatContextBadge chatContextMenu chatFileDropOverlay
    chatFileDropSub chatFileDropTitle chatHeaderActions chatMentionMenu chatMetaDot1
    chatMetaOutlineBtn chatMetaSub chatMoreHeaderBtn chatOutlineBackdrop chatOutlineList
    chatOutlinePopover chatOutlineRail chatOutlineSearchClearBtn chatOutlineSearchInput
    chatOutlineSheetGrabber chatOutlineTurnCount chatgptModal clearChatIconBtn
    clearInactiveProcessesBtn clearUnreadDoneBtn closeHostServiceLogsBtn closeSidebarBtn
    closeUserEditorBtn compactContextBtn compactContextLabel composerBox composerModelHintBar
    composerQueueBar composerSection confirmArchiveInsteadOfDeleteBtn confirmCreateProjectBtn
    confirmDeleteProjectBtn confirmExportBtn confirmHandoffNewTabBtn confirmMoveChatBtn
    confirmPermanentDeleteChatBtn confirmPinProjNoteBtn confirmRenameChatBtn confirmRenameProjectBtn
    contextDraftTag contextMeterBar contextMeterFill contextMeterInfoBtn contextMeterValue
    copyHostServiceLogsBtn copyProjPrimaryPathBtn copyQrUrlBtn copySelectionQuickBtn cpDescInput
    cpDesktopMigrateCallout cpExecModeSelect cpMembersChecklist cpNameInput cpSandboxNotice
    cpScopeGroup cpScopeSwitchWrap cpSharedOptionsPane cpStorageLocSelect cpStoragePathPreview
    cpUpgradeDockerCheck cpUpgradeDockerWrap createProjectModal ctxProjHostServicesBtn
    ctxProjSettingsBtn ctxProjShareBtn customChatIconInput deleteOrArchiveChatHeading
    deleteOrArchiveChatHint deleteOrArchiveChatModal deleteOrArchiveUnreportedWarn
    deleteProjectArchiveHint deleteProjectChats deleteProjectConfirmInput deleteProjectConfirmLabel
    deleteProjectConfirmWrap deleteProjectFiles deleteProjectFilesLabel deleteProjectStats
    desktopComposerNotice desktopMigrationModal desktopReadonlyBanner dismissVoiceHttpsBtn
    dmExecuteBtn dmImportChatsChk dmMergeConflictChk dmMoveSiblingsChk dmProjectsList
    dmRebuildVenvChk dmRescanBtn dmResultBox dmSelectActionableBtn dmSelectAllBtn dmSelectNoneBtn
    dmSummaryPill dmTargetRootPath dockerOfflineBanner dockerOfflineBannerText emptySkeletonBanner
    existingFoldersChips exportContentArea exportIdeaTitle exportModal exportProjectSelect
    exportTargetFile exportWriteMode fileUploadInput foldersContainer forwardTargetBackdrop
    forwardTargetPopover generateProjMemLabel generateProjMemSpinner generateProjectMemoryBtn
    globalProxyActiveBadge globalProxyCustomBox globalProxyDetectedInfo globalProxyModeGroup
    globalProxySpecInput globalProxyTestResult handoffAccountPickerRow handoffAdvisorSlot
    handoffCustomIconInput handoffExistingTargetsWrap handoffExportDocBtn handoffIconGrid
    handoffIconPickerBox handoffModalHeading handoffModalProjBadge handoffModalProjDot
    handoffModalProjName handoffModalSub handoffModeFullBtn handoffModeRow handoffModeWriteBtn
    handoffModelBtn handoffModelDot handoffModelMenu handoffModelName handoffModelSourceTag
    handoffModelTier handoffModelWrap handoffNewRecPill handoffNewTabConfigBox handoffNewTabModal
    handoffOrBalanceBadge handoffOrBoostersBox handoffOrCouncilChk handoffOrReviewChk
    handoffQuotaPills handoffRandomIconBtn handoffResetTitleBtn handoffSelectedIconPreview
    handoffTargetNewCard handoffTargetNewWrap handoffTargetsList handoffTargetsMeta
    handoffTitleInput helpPaneModes helpPaneProjects helpPaneTips hljsTheme hostServiceCreateDetails
    hostServiceLiveTailBadge hostServiceLogsBox hostServiceLogsHead hostServiceLogsPre
    hostServiceLogsTitle hostServicesBannerHint hostServicesBtn hostServicesBtnDot
    hostServicesBtnLabel hostServicesList hostServicesModal hostServicesModalProjBadge
    hostServicesModalTitle hostServicesScopeTabs hsAllowGpuWrap hsNewAllowGpuChk hsNewCmdInput
    hsNewNameInput hsNewPortInput hsStartNewBtn iconPickerGrid iconPickerModal iconPickerTargetTitle
    importDesktopChatBtn importDesktopChatBtnBottom installPwaBtn installXrayCoreBtn lightboxCaption
    lightboxDownloadBtn lightboxImg lightboxOpenRawBtn lightboxStage liveVoiceBar liveVoiceBtn
    liveVoiceEndBtn liveVoiceFlushBtn liveVoiceModelSelect liveVoiceMuteBtn liveVoiceOrb
    liveVoiceRateBtn liveVoiceStatusText liveVoiceSubtitle liveVoiceTimer liveVoiceVoiceBtn
    liveVoiceWaveCanvas loginError loginForm loginOverlay loginThemeBtn loginUsernameInput logoutBtn
    mainWorkspace mediaLightboxModal mentionChatBtn messagesContainer messagesList mobChatGptDot
    mobChatGptPillBtn mobChatGptText mobOrDot mobOrPillBtn mobOrText mobQuotaDot mobQuotaPillBtn
    mobQuotaText mobTelemetryChevron mobTelemetryExpandBtn mobileAccessBar mobileAccessProjBtn
    mobileAccessProjName mobileAccessSeg mobileHostServicesBtn mobileHostServicesBtnDot
    mobileHostServicesBtnLabel mobileNewChatBtn mobileOutlineBookmarkDot mobileOutlinePill
    mobileOutlinePillText mobileRegularChatTag mobileResetSandboxBtn mobileRightToolsSlot
    mobileSegFullAccessBtn mobileTelemetryBar mobileTopKicker modelPickerAccBadge modelPickerBtn
    modelPickerDot modelPickerLabel modelPickerMenu modelPickerWrap modelSelect modesCheatTable
    modesHelpModal modesHelpTabs moveChatFolderPane moveChatModal moveChatProjectPane
    moveChatTargetTitle moveDestKindGroup moveFolderInput moveProjectSelect msgSelectBar
    msgSelectionToolbar newAccountProfileBtn newActionMenuBtn newActionPopover newChatBtn
    newChatBtnLabel newPinInput newProjectBranchTopBtn newRegularChatBtn oldPinInput
    openChatGptFromSettingsBtn openCliAuthBtn openCreateProjectBtn openCreateProjectLabel
    openCreateSharedFromAdminBtn openDesktopMigrationBtn openDesktopMigrationFromCreateBtn
    openHostServicesFromSettingsBtn openHttpsVoiceLink openModesHelpBtn openNewUserFormBtn
    openOrFromQuotaBtn openOrFromSettingsBtn openProjectMemoryBtn openQrBtn openQuotaFromSettingsBtn
    openSettingsBtn openUsersAdminBtn openUsersFromSettingsBtn openUsersLimitsFromQuotaBtn
    openrouterModal optimizeStorageBtn orApiKeyInput orBalanceBadge orBalanceBarFill
    orBalanceBarWrap orBalancePrimary orBalanceSub orCardBalance orCardKeyState orCardSpend
    orClearLogsBtn orClearModelBtn orCurrentMaskPill orDailyLimitInput orDefaultModelInput
    orDeleteKeyBtn orHeaderBridgeBadge orKeyLabelSub orKeyMaskDisplay orLiveBanner orLogsList
    orMaxPriceInput orModelSuggestList orModelsCountHint orPaneConfig orPaneTelemetry orPresetsBar
    orQuickToggleBtn orRebuildExistingAiTitlesBtn orRefreshBtn orRegenEligibleHint orRegenTitlesBtn
    orRulesCounter orRulesTextarea orSaveBottomBtn orSaveConfigBtn orSaveStatusHint orShowcaseGrid
    orSmartTitlesBox orSmartTitlesControls orSmartTitlesModelInput orSmartTitlesPresetsBar
    orSmartTitlesSpendPill orSmartTitlesToggle orSpendCallsPill orSpendPrimary orSpendSub
    orTabsSwitch orTechSummaryGrid orTierBadge orToggleKeyVisBtn orUpdatedTime orUsedModelsBody
    orUsedModelsEmpty orUsedModelsWrap outlineBookmarkCount outlineCloseMobileBtn
    outlineFilterAllBtn outlineFilterBookmarksBtn outlinePinBtn outlineSyncActiveBtn
    outlineSyncActiveLabel outlineTurnPreviewCard paletteGrid pendingForwardPreview
    pendingHqReturnPreview pendingReplyPreview pinChatBtn pinInput pinProjNoteModal pinProjNoteSub
    pinProjNoteTextarea pinProjTargetGroup procScopeAllCount projExtraDirInput projExtraDirsList
    projExtraDirsStatus projImportChatsChk projImportFolderDetails projImportRebuildVenvChk
    projImportSourceInput projMemFileMapInput projMemNewNoteInput projMemOverviewInput
    projMemPaneChangelog projMemPanePassport projMemPaneSettings projMemRulesInput
    projMemTabSettingsBtn projMemoryChangelogList projMemoryExtraCount projMemoryHowItWorksBtn
    projMemoryLogCount projMemoryModalTitle projMemorySyncStatus projMemoryTabs
    projRunImportFolderBtn projSettingsHealthBadge projSettingsHostSummary projSettingsMigratedList
    projSettingsMigratedWrap projSettingsPrimaryPath projectContextMenu projectMemoryModal
    projectMemoryStatusDot projectModeChips projectsTreeCount projectsTreeList promptInput
    qmAccountEmail qmAccountPlanTag qmAccountRoutePill qmActiveChatTokens qmAdminAccountCard
    qmAvatar qmLiveQuotasSection qmOpenRouterCard qmOrDot qmOrSummaryText qmOrTitle qmTierSub
    qmTokensAnalyticsSection qmUpdatedTime qrModal qrSvgContainer qrUrlSelect qrUrlText
    quickStarters quotaGroupsGrid quotaModal quotaModalHeading quoteSelectionQuickBtn
    recentChatsList recheckDockerBannerBtn referencedChatsPreview refreshAppBtn
    refreshHostServiceLogsBtn refreshHostServicesBtn refreshQuotasBtn regularChatsArchiveWrap
    regularChatsCount renameChatInput renameChatModal renameProjectInput resetSandboxBtn
    restartServerBtn sacAccountName sacAuthDot sacClaudeFill sacClaudePct sacGemFill sacGemPct
    sacSubline sacSwitchAccBtn sacTokensToday saveAccountProfileBtn saveGlobalProxyBtn savePinBtn
    saveProjExtraDirsBtn saveProjectMemoryBtn saveUserBtn saveUserRulesBtn savedProfilesBar
    scgBar5hFill scgBar5hLabel scgBar5hVal scgBar7dFill scgBar7dLabel scgBar7dVal scgCardName
    scgCardView scgCompactDot scgCompactView scgRightMeta scgStatusDot scgSubline scgSwitchAccBtn
    scgTitleText scgTokensPill scrollBottomBtn searchInput securityDot securityModeWrap
    segFullAccessBtn selToolbarCopyBtn selToolbarPinMemBtn selToolbarReplyBtn selToolbarSep
    selToolbarSepPin sendBtn sendProcessLogsToAiBtn sendToCodeBackdrop sendToCodeMenu
    settingsAccountBadge settingsCgptStatusBadge settingsCgptSubHint settingsChatGptBlock
    settingsModal settingsModelAdvisorBlock settingsOpenRouterBlock settingsOrStatusBadge
    settingsOrSubHint shareProjectForm shareProjectModal sharedProjectsAdminList sidebar
    sidebarAccountCard sidebarActivityList sidebarActivitySection sidebarBackdrop sidebarChatGptBtn
    sidebarFilterBar sidebarOpenRouterBtn sidebarProcessesStrip sidebarResizer sidebarRoleBadge
    sidebarScroll sidebarToggleAlertDot sidebarToggleRadar skeletonExtraDirsBtn
    skeletonImportFolderBtn sorCardName sorCardView sorCol1Fill sorCol1Label sorCol1Val sorCol2Fill
    sorCol2Label sorCol2Val sorCompactDot sorCompactView sorRightMeta sorStatusDot sorSubline
    sorTitleText sorTokensPill stopBtn storageHealthSummary syncAccountBtn sysInfoGrid
    tailscaleBanner testGlobalProxyBtn themeColorMeta themeModeGroup themeToggleBtn toastContainer
    toggleAccountEditBtn toggleAccountRouteBtn toggleSidebarBtn tokenModelsBody tokenModelsHead
    tokenModelsWrap tokenSectionHeading tokenSummaryGrid tokenUsersBody tokenUsersCountBadge
    tokenUsersHead tokenUsersWrap topAccessSeg treeFilterBadgesAll treeFilterBadgesChats
    treeFilterBadgesProjects treeFilterBtnAll treeFilterBtnChats treeFilterBtnProjects
    treeProjectsSection treeRegularChatsSection ueAllowAllModelsBtn ueDailyLimitInput
    ueDisplayNameInput ueHostFullHint ueMaxModeSelect ueModelsChecklist ueNoOpusModelsBtn
    uePasswordInput uePasswordLabel ueReservePctInput ueSandboxNetSelect ueSandboxNetWrap
    ueUserIdInput ueUsernameInput unarchiveActiveChatBtn unloadTabMemoryBtn updateCliBtn
    userEditorBox userEditorHeading userRulesCounter userRulesSection userRulesStatusHint
    userRulesTextarea usersAdminList usersAdminModal voiceAutoSendChk voiceAutoStopChk voiceBtn
    voiceCancelBtn voiceDoneBtn voiceEngineSelect voiceHttpsBanner voiceLangSelect voiceMicSelect
    voiceModelLabel voiceModelSelect voiceRecDot voiceRecordingBar voiceSmartFormatChk
    voiceStatusLabel voiceStatusPill voiceTimer voiceWaveCanvas voiceWaveform welcomeContextSwitch
    welcomeHeading welcomeProjPopover welcomeState welcomeSubtext workspaceTabbar wsDot
    wsStatusBadge wsStatusText xrayCoreStatusPill
    createGitCheckpointBtn ctxProjGitBtn discardGitUncommittedBtn gitCheckpointMsgInput
    initProjectGitBtn installProjectShieldBtn mobileProjectGitBadge mobileProjectGitBtn
    openProjectGitBtn projDisableGuardrailsChk projGuardrailsCard projGuardrailsStatusBadge
    projectGitBadge projectGitBranchBadge projectGitChangedList projectGitCheckpointsList
    projectGitCleanBanner projectGitCreateRow projectGitDirtyBox projectGitDraftActionsRow
    projectGitEmptyInitBox projectGitHistoryCount projectGitInitReadonlyHint
    projectGitMainContent projectGitModal projectGitModalProjBadge projectGitModalTitle
    projectGitShieldBar projectGitShieldStatusText projectGitStashesPill
    projectGitWorkingCount projectGitWorkingDiffWrap refreshProjectGitBtn
    toggleAllWorkingDiffBtn
    agyConfirmCancelBtn agyConfirmMessage agyConfirmModal agyConfirmOkBtn agyConfirmTitle
    handoffAutoCheckpointChk handoffGitCheckpointBox handoffGitCheckpointLabel
    handoffGitCheckpointSub
    disconnectGdriveBtn exchangeGdriveCodeBtn gdriveAccountEmail gdriveAutoSyncChk
    gdriveClientEditWrap gdriveClientSavedHint gdriveConnBadge gdriveConnectActionsBox
    gdriveConnectedBar gdriveExcludedProjectsDetails gdriveIncludeEnvVaultChk
    gdriveLastSyncText gdriveManualCallbackInput gdriveModal gdriveModalTitle
    gdriveOauthClientBox gdriveOauthJsonInput gdrivePreviewBox gdrivePreviewSummary
    gdriveProjectsChecklist gdriveProjectsCountLabel gdriveRootFolderLabel
    gdriveSettingsCard gdriveSettingsStatusPill gdriveSkippedCount gdriveSkippedDetails
    gdriveSkippedList gdriveSyncSelfHubChk gdriveVaultPassInput gdriveVaultPassRow
    gdriveVaultPassStatusBadge openGdriveModalBtn previewGdriveSyncBtn quickGdriveSyncBtn
    refreshGdriveModalBtn runGdriveSyncBtn saveGdriveClientBtn saveGdriveSettingsBtn
    startGdriveOauthBtn toggleGdriveClientEditBtn
    gdriveRestoreCatalogBox gdriveRestoreCodeChk gdriveRestoreDetails gdriveRestoreDialogsChk
    gdriveRestoreOverwriteChk gdriveRestoreProjectsChecklist gdriveRestoreResultBox
    gdriveRestoreSummaryText gdriveRestoreVaultChk gdriveRestoreVaultPassInput
    gdriveRestoreVaultPassWrap loadGdriveRestoreCatalogBtn runGdriveRestoreBtn
    gdriveRootFolderInput gdriveSyncCurrentItem gdriveSyncEtaText gdriveSyncProgressBar
    gdriveSyncProgressBox gdriveSyncProgressFill gdriveSyncProgressText
    sidebarUpdateBannerBtn updatesSettingsCard updatesCurrentVerBadge updatesSettingsStatusPill
    updatesSettingsHint openUpdatesModalBtn quickCheckUpdatesBtn updatesModal
    updModalVerBadge updRepoExternalLink updTabsBar updTabSystemBtn
    updTabPublishBtn updPaneSystem updCurrentVerText updLatestVerText
    updLastCheckedText updCheckBtn updReleaseInfoBox updReleaseStatusText
    updFirstPublishBtn updReleaseDetails updReleaseTitle updReleaseDate
    updReleaseNotes updApplyBtn updApplyProgressBox updApplyProgressText
    updPanePublish pubOwnerInput pubRepoInput pubTokenMaskPill
    pubTokenInput pubToggleTokenBtn pubSaveConfigBtn pubVersionInput
    pubTitleInput pubNotesTextarea pubFillDraftBtn pubShieldNotice
    pubSubmitReleaseBtn pubResultBanner
    ctxProjEnvBtn projEnvAddRowBtn projEnvExampleBar projEnvExampleHint projEnvExportExampleBtn
    projEnvFilePills projEnvImportExampleBtn projEnvModeRawBtn projEnvModeTableBtn
    projEnvNewCommentInput projEnvNewKeyInput projEnvNewValInput projEnvRawMaskOverlay
    projEnvRawTextarea projEnvRawWrap projEnvRevealAllBtn projEnvRowsList projEnvSaveStatus
    projEnvSearchInput projEnvSecurityBanner projEnvTableWrap projMemPaneEnv projMemTabEnvBtn
    projMemoryEnvCount projSecretsFilesCard projSecretsFilesCount projSecretsFilesList
    saveProjEnvBtn
""".split())


class ShellParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = Counter()
        self.assets = []

    def handle_starttag(self, tag, attrs):
        attrs_by_name = dict(attrs)
        self.ids.update(value for name, value in attrs if name == "id")
        if tag == "link" and "stylesheet" in attrs_by_name.get("rel", "").split():
            self.assets.append(attrs_by_name.get("href", ""))
        if tag == "script":
            self.assets.append(attrs_by_name.get("src", ""))


NODE_CONTRACTS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = fs.readFileSync("app/static/app.js", "utf8");
const ids = JSON.parse(fs.readFileSync(0, "utf8"));

function inspectSources() {
  // Node ships Acorn for its own tooling. Load that copy without installing npm
  // packages or enabling internal modules for the entire test process.
  const parser = process.binding("natives")["internal/deps/acorn/acorn/dist/acorn"];
  assert.ok(parser, "This gate requires a Node distribution with bundled Acorn");
  const acorn = { exports: {} };
  vm.runInNewContext(parser, { module: acorn, exports: acorn.exports });
  const parse = source => acorn.exports.parse(source, { ecmaVersion: "latest", locations: true });

  const globals = new Set((
    "setTimeout clearTimeout setInterval clearInterval requestAnimationFrame cancelAnimationFrame " +
    "queueMicrotask requestIdleCallback cancelIdleCallback fetch URL URLSearchParams Blob File FileReader " +
    "FormData Headers Request Response AbortController AbortSignal WebSocket Audio AudioContext " +
    "MediaRecorder MutationObserver ResizeObserver IntersectionObserver CustomEvent Event Promise Map Set " +
    "AudioWorkletNode SpeechSynthesisUtterance WeakMap WeakSet Date RegExp Error TypeError RangeError DOMException Number String Boolean Array Object " +
    "Symbol BigInt Uint8Array Int16Array Float32Array ArrayBuffer DataView TextEncoder TextDecoder " +
    "parseInt parseFloat isNaN isFinite encodeURI decodeURI encodeURIComponent decodeURIComponent " +
    "atob btoa structuredClone alert confirm prompt getComputedStyle matchMedia marked hljs AgyUI"
  ).split(/\s+/));

  function inspect(source) {
    const calls = [];
    const lookups = new Set();
    const rawConfirms = [];
    function scope(parent, varScope = false) {
      return { parent, varScope, names: new Set() };
    }
    function bind(pattern, names) {
      if (!pattern) return;
      switch (pattern.type) {
        case "Identifier": names.add(pattern.name); break;
        case "RestElement": bind(pattern.argument, names); break;
        case "AssignmentPattern": bind(pattern.left, names); break;
        case "ArrayPattern": pattern.elements.forEach(item => bind(item, names)); break;
        case "ObjectPattern":
          pattern.properties.forEach(item => bind(item.type === "Property" ? item.value : item, names));
          break;
      }
    }
    const functions = new Set(["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"]);
    const blocks = new Set([
      "BlockStatement", "SwitchStatement", "CatchClause", "ForStatement",
      "ForInStatement", "ForOfStatement", "StaticBlock",
    ]);

    function visit(node, current, fnOwner = null) {
      const { type } = node;
      let nextFnOwner = fnOwner;
      if (type === "FunctionDeclaration" || type === "ClassDeclaration") bind(node.id, current.names);
      if (functions.has(type)) {
        current = scope(current, true);
        bind(node.id, current.names);
        node.params.forEach(param => bind(param, current.names));
        nextFnOwner = node.id?.name ?? null;
      } else if (blocks.has(type) || type === "ClassExpression") {
        current = scope(current, type === "StaticBlock");
        bind(node.param ?? node.id, current.names);
      }
      if (type === "VariableDeclaration") {
        let target = current;
        if (node.kind === "var") while (!target.varScope) target = target.parent;
        node.declarations.forEach(item => bind(item.id, target.names));
      }
      if (type === "CallExpression" || type === "NewExpression") {
        const { callee } = node;
        if (callee.type === "Identifier") {
          calls.push({ name: callee.name, line: callee.loc.start.line, current });
        }
        if (type === "CallExpression") {
          const isRawConfirm =
            (callee.type === "Identifier" && callee.name === "confirm") ||
            (callee.type === "MemberExpression" && !callee.computed &&
              callee.object.type === "Identifier" &&
              ["window", "globalThis", "self"].includes(callee.object.name) &&
              callee.property.name === "confirm");
          if (isRawConfirm) {
            rawConfirms.push({ line: callee.loc.start.line, owner: nextFnOwner });
          }
        }
        const isLookup = callee.type === "Identifier"
          ? ["$", "getElementById"].includes(callee.name)
          : callee.type === "MemberExpression" && !callee.computed && callee.property.name === "getElementById";
        if (isLookup && typeof node.arguments[0]?.value === "string") lookups.add(node.arguments[0].value);
      }
      for (const value of Object.values(node)) {
        for (const child of Array.isArray(value) ? value : [value]) {
          if (child && typeof child.type === "string") visit(child, current, nextFnOwner);
        }
      }
    }
    visit(parse(source), scope(null, true), null);
    const unknown = [];
    for (const { name, line, current } of calls) {
      if (globals.has(name)) continue;
      let owner = current;
      while (owner && !owner.names.has(name)) owner = owner.parent;
      if (!owner) unknown.push({ name, line });
    }
    return { unknown, lookups: [...lookups], rawConfirms };
  }

  const injectedCall = "nonExistentRenamedUiHelper_v999";
  const sample = [
    "/* notACall() */ // neitherIsThis()",
    "const text = 'quoted()'; const regex = /regex\\\\(\\\\)/; const quotient = 20 / 2;",
    "const fn = ({run = () => 1}, ...rest) => run() / rest[0]();",
    "class Fixture { method() {} } const item = { method() {} }; item?.method();",
    "const template = `literalCall() ${fn({})} ${\"quoted()\"} ${" + injectedCall + "()}`;",
  ].join("\n");
  assert.deepEqual(inspect(sample).unknown, [{ name: injectedCall, line: 5 }]);
  assert.deepEqual(
    inspect("function bad() { confirm('a'); window.confirm('b'); }").rawConfirms,
    [{ line: 1, owner: "bad" }, { line: 1, owner: "bad" }]
  );
  const lines = app.split("\n").length;
  const injected = inspect(app + "\n" + injectedCall + "();").unknown;
  assert.deepEqual(injected.at(-1), { name: injectedCall, line: lines + 1 });

  const sw = parse(fs.readFileSync("app/static/sw.js", "utf8"));
  const declarations = sw.body.flatMap(statement => statement.declarations ?? []);
  const getInitializer = name => declarations.find(item => item.id.name === name)?.init;
  const cache = getInitializer("CACHE_NAME")?.value;
  const shell = getInitializer("SHELL_ASSETS")?.elements.map(item => item.value);
  console.log(JSON.stringify({ ...inspect(app), cache, shell }));
}

function smoke(width, height, userAgent) {
  const noop = () => {};
  function element(id) {
    return {
      id, value: "", textContent: "", innerHTML: "", options: [], dataset: {}, children: [],
      classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
      style: { setProperty: noop, removeProperty: noop, getPropertyValue: () => "" },
      addEventListener: noop, removeEventListener: noop, setAttribute: noop, removeAttribute: noop,
      getAttribute: () => null, appendChild: noop, replaceChildren: noop,
      querySelector: () => null, querySelectorAll: () => [],
    };
  }
  const elements = new Map(ids.map(id => [id, element(id)]));
  const storage = () => {
    const items = new Map();
    return {
      getItem: key => items.get(key) ?? null,
      setItem: (key, value) => items.set(key, String(value)),
      removeItem: key => items.delete(key),
    };
  };
  const document = {
    documentElement: element(), body: element(),
    getElementById: id => elements.get(id) ?? null,
    querySelector: selector => selector.startsWith("#") ? elements.get(selector.slice(1)) ?? null : null,
    querySelectorAll: () => [],
    createElement: () => element(), addEventListener: noop,
  };
  const context = {
    __AGY_EXPORT_ADVISOR_FOR_TESTS__: true, __AGY_SKIP_AUTO_INIT__: true,
    document, navigator: { userAgent }, innerWidth: width, innerHeight: height,
    location: new URL("https://hub.invalid/"), URL, URLSearchParams,
    localStorage: storage(), sessionStorage: storage(),
    addEventListener: noop, removeEventListener: noop,
    setTimeout: noop, clearTimeout: noop, setInterval: noop, clearInterval: noop,
    requestAnimationFrame: noop, cancelAnimationFrame: noop,
    CSS: { supports: () => true, escape: String },
    matchMedia(query) {
      const limit = query.match(/\((max|min)-width:\s*(\d+)px\)/);
      const matches = limit && (limit[1] === "max" ? width <= +limit[2] : width >= +limit[2]);
      return { matches: Boolean(matches), addEventListener: noop, removeEventListener: noop };
    },
  };
  context.window = context;
  const sandbox = vm.createContext(context);
  if (process.argv[1] === "smoke-foundation") {
    const ui = fs.readFileSync("app/static/ui-shell.js", "utf8");
    vm.runInContext(ui, sandbox, { filename: "ui-shell.js", timeout: 5000 });
    assert.equal(typeof context.AgyUI?.positionPopover, "function");
  }
  vm.runInContext(app, sandbox, { filename: "app.js", timeout: 5000 });
  const api = context.__AGY_ADVISOR_API__;
  assert.ok(api, "Missing Advisor test API");
  const dataExports = new Set(["els", "state", "mentionStatsCache", "tabViewCache", "COPY_STRIP_SELECTOR"]);
  for (const [name, method] of Object.entries(api)) {
    if (dataExports.has(name)) continue;
    assert.equal(typeof method, "function", name);
  }
  for (const name of [
    "syncSecurityModeUI", "syncAccessSelectStyle", "syncModelPickerUI", "syncComposerState",
    "renderContextMeter", "syncBackgroundAlertsUI", "syncSidebarFilterBarBadges",
    "renderUnifiedSidebar", "renderChatActiveProcessesBar", "pruneStaleUnreadAndRunningState",
  ]) {
    assert.doesNotThrow(() => api[name](), width + "x" + height + " " + name);
  }
}

if (process.argv[1] === "analyze") {
  inspectSources();
} else {
  smoke(1440, 900, "Mozilla/5.0 (Windows NT 10.0; Win64; x64)");
  smoke(390, 844, "Mozilla/5.0 (Linux; Android 14; Mobile)");
}
"""


@pytest.fixture(scope="module")
def shell():
    parser = ShellParser()
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    parser.close()
    return parser


def run_node(mode, ids=(), *, script=NODE_CONTRACTS):
    result = subprocess.run(
        ["node", "-e", script, mode], input=json.dumps(list(ids)),
        cwd=ROOT, capture_output=True, encoding="utf-8", timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


@pytest.fixture(scope="module")
def sources():
    return json.loads(run_node("analyze"))


def test_dom_ids(shell, sources):
    duplicates = sorted(name for name, count in shell.ids.items() if count > 1)
    assert not duplicates, f"Duplicate HTML IDs: {duplicates}"
    missing_critical = CRITICAL_IDS - shell.ids.keys()
    assert not missing_critical, f"Missing structural IDs: {sorted(missing_critical)}"
    linked = shell.ids.keys() & set(sources["lookups"])
    missing_linked = LINKED_IDS - linked
    assert not missing_linked, f"Missing HTML IDs or JS lookups: {sorted(missing_linked)}"
    assert len(linked) >= MIN_LINKED_IDS, f"Only {len(linked)} linked IDs remain"


def test_pwa_assets(shell, sources):
    cache = sources.get("cache", "")
    version = cache.removeprefix("agy-hub-v")
    assert cache.startswith("agy-hub-v") and version.isascii() and version.isdecimal(), cache
    assets = sources.get("shell")
    assert assets and all(isinstance(url, str) for url in assets), assets
    assert {"/", "/manifest.json"} <= set(assets)
    expected = {f"/static/{filename}" for filename in (
        "styles.css", "ui-system.css", "ui-shell.js", "app.js",
    )}
    # Versioned assets must appear on both sides, including future CSS/JS files.
    for urls in (shell.assets, assets):
        for url in urls:
            parts = urlsplit(url)
            if (not parts.scheme and not parts.netloc and parts.path.startswith("/static/")
                    and parts.path.endswith((".css", ".js"))
                    and "v" in parse_qs(parts.query, keep_blank_values=True)):
                expected.add(parts.path)
    for path in sorted(expected):
        for label, urls in (("index.html", shell.assets), ("SHELL_ASSETS", assets)):
            matches = [url for url in urls if urlsplit(url).path == path]
            assert matches == [f"{path}?v={version}"], f"{label}: {path} expected v={version}, got {matches}"
    for url in assets:
        parts = urlsplit(url)
        if parts.scheme or parts.netloc or parts.path == "/":
            continue
        versions = parse_qs(parts.query, keep_blank_values=True).get("v")
        assert versions is None or versions == [version], f"{url} differs from {cache}"
        assert parts.path == "/manifest.json" or parts.path.startswith("/static/"), url
        relative = parts.path.removeprefix("/static/") if parts.path != "/manifest.json" else "manifest.json"
        file = STATIC / relative
        assert file.resolve().is_relative_to(STATIC), url
        assert file.is_file() and file.stat().st_size > 0, f"Missing or empty shell asset: {url}"


def test_direct_calls_are_declared(sources):
    unknown = [f"app.js:{call['line']}: {call['name']}" for call in sources["unknown"]]
    assert not unknown, "Unresolved direct calls:\n" + "\n".join(unknown)


def test_no_raw_browser_confirm_outside_wrapper(sources):
    leaked = [
        f"app.js:{entry['line']} (in {entry['owner'] or '<top-level>'})"
        for entry in sources.get("rawConfirms", [])
        if entry.get("owner") != "askUserConfirm"
    ]
    assert not leaked, "Direct browser confirm() calls outside askUserConfirm:\n" + "\n".join(leaked)


def test_ui_synchronizers(shell):
    run_node("smoke", shell.ids)


def test_css_important_budget():
    for asset, limit in (("styles.css", 97), ("ui-system.css", 1)):
        source = (STATIC / asset).read_text(encoding="utf-8")
        declarations = re.sub(r"/\*[\s\S]*?\*/", "", source)
        count = len(re.findall(r"!\s*important\b", declarations, flags=re.IGNORECASE))
        assert count <= limit, f"{asset}: {count} !important declarations, maximum {limit}"
