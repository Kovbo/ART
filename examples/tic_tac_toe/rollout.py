import art
import openai
import time
import math
import os
from dotenv import load_dotenv
from pydantic import BaseModel
from openpipe.client import OpenPipe

from game_utils import (
    generate_game,
    get_opponent_move,
    apply_agent_move,
    check_winner,
    render_board,
)

load_dotenv()

op_client = OpenPipe()
print("OpenPipe client initialized")


op_client = OpenPipe(api_key=os.getenv("OPENPIPE_API_KEY"))


class TicTacToeScenario(BaseModel):
    step: int


@art.retry(exceptions=(openai.LengthFinishReasonError,))
async def rollout(model: art.Model, scenario: TicTacToeScenario) -> art.Trajectory:
    game = generate_game()

    trajectory = art.Trajectory(
        messages_and_choices=[
            {
                "role": "system",
                "content": f"You are a tic-tac-toe player. You are playing against an opponent. Always choose the move most likely to lead to an eventual win. Return your move as an XML object with a single property 'move', like so: <move>A1</move>. Optional moves are 'A1', 'B3', 'C2', etc. You are the {game['agent_symbol']} symbol.",
            }
        ],
        reward=0,
    )

    move_number = 0
    invalid_move = False

    if game["agent_symbol"] == "o":
        starting_opponent_move = get_opponent_move(game)
        game["board"][starting_opponent_move[0]][starting_opponent_move[1]] = game[
            "opponent_symbol"
        ]

    while check_winner(game["board"]) is None:
        trajectory.messages_and_choices.append(
            {"role": "user", "content": render_board(game)}
        )

        requested_at = int(time.time() * 1000)
        messages = trajectory.messages()

        try:
            client = model.openai_client()
            chat_completion = await client.chat.completions.create(
                model=model.get_inference_name(),
                messages=messages,
                max_completion_tokens=128,
            )
        except openai.LengthFinishReasonError as e:
            raise e
        except Exception as e:
            print("caught exception generating chat completion")
            print(e)
            global failing_trajectory
            failing_trajectory = trajectory
            raise e

        choice = chat_completion.choices[0]
        content = choice.message.content
        assert isinstance(content, str)
        trajectory.messages_and_choices.append(choice)

        try:
            apply_agent_move(game, content)
        except ValueError:
            invalid_move = True
            trajectory.reward = -100 + (math.log(move_number + 1) / math.log(100))
            break

        move_number += 1
        if check_winner(game["board"]) is not None:
            break

        opponent_move = get_opponent_move(game)
        game["board"][opponent_move[0]][opponent_move[1]] = game["opponent_symbol"]

    winner = check_winner(game["board"])

    if winner == game["agent_symbol"]:
        trajectory.reward = 1
        trajectory.metrics["win"] = 1
    elif winner == game["opponent_symbol"]:
        trajectory.reward = 0
        trajectory.metrics["win"] = 0
    elif winner == "draw":
        trajectory.reward = 0.5
        trajectory.metrics["win"] = 0.5

    trajectory.metrics["num_moves"] = move_number
    trajectory.metrics["invalid_move"] = 1 if invalid_move else 0

    if op_client.api_key:
        try:
            reported_win = (
                trajectory.metrics["win"] if "win" in trajectory.metrics else -1
            )
            op_client.report(
                requested_at=requested_at,
                received_at=int(time.time() * 1000),
                req_payload={
                    "model": model.name,
                    "messages": messages,
                    "metadata": {
                        "notebook-id": "tic-tac-toe",
                        "step": str(scenario.step),
                        "num_moves": str(move_number),
                        "win": str(reported_win),
                        "reward": str(trajectory.reward),
                        "invalid_move": str(invalid_move),
                    },
                },
                resp_payload=chat_completion,
                status_code=200,
            )
        except Exception as e:
            print(f"Error reporting to OpenPipe: {e}")

    return trajectory
