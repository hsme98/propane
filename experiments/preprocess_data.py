from reconstruction.reconstruct import Reconstructor
import reconstruction.common as common

from argparse import ArgumentParser
import os
import json
import random
from pathlib import Path
import pickle


def reconstructor_worker(
    rec: Reconstructor,
    prompts: list[tuple[int, str]],
    max_len: int,
    n_docs: int,
    out_path: str,
) -> list[dict]:
    """
    Worker process: generate datasets from prompts

    Parameters
    ----------
        rec: Reconstructor
            Reconstructor object
        prompts: list[tuple[int, str]]
            List of prompts to generate datasets from (id and prompt)
        max_len: int
            Number of tokens to generate for each document
        n_docs: int
            Number of documents to generate for each prompt
        out_path: str
            Path to save datasets to

    Returns
    -------
        list[dict]
            List of results from Reconstructor.train()
    """

    return rec.gen_datasets_from_prompts(
        prompts,
        max_len,
        n_docs,
        out_path,
        "alpaca",
        False,
        True,
        True,
    )


def process_alpaca(
    dataset_path: str,
    num_samples: int,
) -> list[tuple[int, str]]:
    """
    Process the Alpaca instruction dataset

    Parameters
    ----------
        dataset_path: str
            Path to the dataset
        num_samples: int
            Number of samples to randomly select from the dataset

    Returns
    -------
        list[tuple[int, str]]
            List of prompts (id and prompt)
    """

    data = json.load(open(dataset_path, "r"))

    # Filter out everything that has context
    data = [d for d in data if d["input"] == ""]
    data = random.sample(data, num_samples)
    data_with_ids = [{"id": i, **d} for i, d in enumerate(data)]
    prompts = [(d["id"], d["instruction"]) for d in data_with_ids]

    return prompts


def process_hellaswag(
    dataset_path: str,
    num_samples: int,
) -> list[tuple[int, str]]:
    data = [json.loads(l) for l in open(dataset_path, "r").readlines()]

    # random.shuffle(data)
    data_with_ids = [{"id": i, **d} for i, d in enumerate(data)]
    prompts = [(d["id"], d) for d in data_with_ids]

    to_ret = []
    activities = set()

    for id, p in prompts:
        # if p["activity_label"] in activities:  # want different types
        #     continue

        activities.add(p["activity_label"])
        to_ret.append((id, p["ctx"]))

        if len(to_ret) == num_samples:
            break

    return to_ret


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--model_name_or_path", type=str)
    parser.add_argument("--raw_dataset_path", type=str)
    parser.add_argument("--output_dir", type=str, default="data")
    parser.add_argument("--num_samples", type=int)
    parser.add_argument("--max_len", type=int, default=32)
    parser.add_argument("--num_docs_per_sample", type=int, default=100)
    parser.add_argument("--batch_size", type=int, default=10)
    parser.add_argument("--fp16", action="store_true")
    parser.add_argument("--init_run", type=bool, default=False)


    args = parser.parse_args()

    if args.init_run:
        # needs to have the HF_HOME director set up properly this will run in the init_node
        assert( "HF_HOME" in list(os.environ.keys()) )
        hf_home_dir = os.environ["HF_HOME"]
        models = common.load_model_tokenizer(args.model_name_or_path, args.fp16, device_map="cpu")
    else:
        pool = common.setup_multiproc_env()
        n_procs = pool._processes
        models, tokenizers = common.load_models_tokenizers_parallel(
            args.model_name_or_path, args.fp16
        )

        if "alpaca" in args.raw_dataset_path:
            prompts = process_alpaca(args.raw_dataset_path, args.num_samples)
        elif "hellaswag" in args.raw_dataset_path:
            prompts = process_hellaswag(args.raw_dataset_path, args.num_samples)
        else:
            raise ValueError(f"Dataset {args.raw_dataset_path} not supported")

        prompts_per_proc: list[list[tuple[int, str]]] = [[] for _ in range(n_procs)]
        reconstructors = []
        prompts_per_proc = common.split_for_multiproc(prompts, n_procs)
        for i in range(n_procs):
            reconstructors.append(
                Reconstructor(
                    models[i],
                    tokenizers[i],
                    args.batch_size,
                    args.max_len,
                )
            )
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)

        print(f"Total prompts: {len(prompts)}")
        print("Generating data...")
        results: list = []

        for i in range(n_procs):
            results.append(
                pool.apply_async(
                    reconstructor_worker,
                    (
                        reconstructors[i],
                        prompts_per_proc[i],
                        args.max_len,
                        args.num_docs_per_sample,
                        args.output_dir,
                    ),
                )
            )

        pool.close()
        pool.join()
        results = [x.get() for x in results]
        results_flat = [x for sublist in results for x in sublist]

        print("Saving data...")
        with open(
            os.path.join(
                args.output_dir,
                f"docs_{args.num_docs_per_sample}_prompts_{args.num_samples}.pkl",
            ),
            "wb",
        ) as f:
            pickle.dump(results_flat, f)
