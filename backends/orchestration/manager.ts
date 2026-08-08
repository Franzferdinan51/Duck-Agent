/**
 * Duck Agent - Orchestration System
 * 
 * Coordinates multi-step workflows and tasks.
 */

import { randomUUID } from 'crypto'

export interface WorkflowStep {
  id: string
  name: string
  /** Function to execute for this step */
  execute: (context: WorkflowContext) => Promise<StepResult>
  /** Dependencies on other steps */
  dependsOn?: string[]
  /** Optional retry configuration */
  retries?: number
  /** Optional timeout in milliseconds */
  timeoutMs?: number
}

export interface StepResult {
  success: boolean
  output?: unknown
  error?: string
  metadata?: Record<string, unknown>
}

export interface WorkflowContext {
  workflowId: string
  variables: Record<string, unknown>
  steps: Record<string, StepResult>
}

export interface Workflow {
  id: string
  name: string
  steps: WorkflowStep[]
  status: 'pending' | 'running' | 'completed' | 'failed'
  createdAt: number
  startedAt?: number
  completedAt?: number
  context: WorkflowContext
}

export interface WorkflowResult {
  workflowId: string
  success: boolean
  steps: Record<string, StepResult>
  totalSteps: number
  completedSteps: number
  failedSteps: number
  duration: number
}

/**
 * Workflow Manager
 * 
 * Orchestrates multi-step workflows for Duck Agent.
 */
export class WorkflowManager {
  private workflows: Map<string, Workflow>

  constructor() {
    this.workflows = new Map()
  }

  /**
   * Create a new workflow
   */
  createWorkflow(name: string, steps: WorkflowStep[]): Workflow {
    const workflow: Workflow = {
      id: randomUUID(),
      name,
      steps,
      status: 'pending',
      createdAt: Date.now(),
      context: {
        workflowId: '',
        variables: {},
        steps: {},
      },
    }
    workflow.context.workflowId = workflow.id
    this.workflows.set(workflow.id, workflow)
    return workflow
  }

  /**
   * Execute a workflow
   */
  async executeWorkflow(workflowId: string): Promise<WorkflowResult> {
    const workflow = this.workflows.get(workflowId)
    if (!workflow) {
      throw new Error(`Workflow not found: ${workflowId}`)
    }

    workflow.status = 'running'
    workflow.startedAt = Date.now()

    // Execute steps in dependency order
    const executed = new Set<string>()
    const remaining = [...workflow.steps]

    let totalSteps = workflow.steps.length
    let completedSteps = 0
    let failedSteps = 0

    while (remaining.length > 0) {
      // Find steps with dependencies satisfied
      const ready = remaining.filter(step =>
        !step.dependsOn || step.dependsOn.every(dep => executed.has(dep))
      )

      if (ready.length === 0) {
        // Circular dependency or missing dependency
        break
      }

      // Execute all ready steps in parallel
      await Promise.all(ready.map(async (step) => {
        let attempts = 0
        const maxAttempts = (step.retries ?? 0) + 1
        let lastError: string | undefined

        while (attempts < maxAttempts) {
          attempts++
          try {
            const timeoutMs = step.timeoutMs ?? 30000
            const result = await Promise.race([
              step.execute(workflow.context),
              new Promise<StepResult>((_, reject) =>
                setTimeout(() => reject(new Error('Step timeout')), timeoutMs)
              ),
            ])

            workflow.context.steps[step.id] = result
            if (result.success) {
              completedSteps++
              executed.add(step.id)
              return
            } else {
              lastError = result.error
            }
          } catch (err) {
            lastError = err instanceof Error ? err.message : String(err)
          }
        }

        workflow.context.steps[step.id] = {
          success: false,
          error: lastError || 'Step failed',
        }
        failedSteps++
        executed.add(step.id)
      }))

      // Remove executed steps
      for (let i = remaining.length - 1; i >= 0; i--) {
        if (executed.has(remaining[i].id)) {
          remaining.splice(i, 1)
        }
      }
    }

    workflow.completedAt = Date.now()
    workflow.status = failedSteps > 0 ? 'failed' : 'completed'

    return {
      workflowId: workflow.id,
      success: workflow.status === 'completed',
      steps: workflow.context.steps,
      totalSteps,
      completedSteps,
      failedSteps,
      duration: workflow.completedAt - workflow.startedAt,
    }
  }

  /**
   * Get a workflow
   */
  getWorkflow(id: string): Workflow | null {
    return this.workflows.get(id) || null
  }

  /**
   * List all workflows
   */
  listWorkflows(): Workflow[] {
    return Array.from(this.workflows.values())
  }

  /**
   * Delete a workflow
   */
  deleteWorkflow(id: string): boolean {
    return this.workflows.delete(id)
  }
}

let managerInstance: WorkflowManager | null = null

export function getWorkflowManager(): WorkflowManager {
  if (!managerInstance) {
    managerInstance = new WorkflowManager()
  }
  return managerInstance
}
