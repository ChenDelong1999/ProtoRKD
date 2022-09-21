import torch
import torch.nn as nn
import numpy as np
import pandas as pd
import logging
import sys

from training.evaluations.nlp_evaluations import nlp_eval
from training.params import parse_args
import open_clip

from transformers import AutoConfig, AutoTokenizer, AutoModel
from sentence_transformers import SentenceTransformer

logging.basicConfig(format='%(asctime)s: %(message)s', level=logging.INFO)

class WrappedHuggingfaceTransformer(nn.Module):
    def __init__(self, huggingface_transformer) -> None:
        super().__init__()        
        config = AutoConfig.from_pretrained(huggingface_transformer)
        self.tokenizer = AutoTokenizer.from_pretrained(huggingface_transformer)
        self.text_backbone = AutoModel.from_pretrained(huggingface_transformer)
        if self.tokenizer.pad_token is None:
            self.tokenizer.add_special_tokens({'pad_token': '[PAD]'})
        self.text_model_builder='huggingface-transformer'
        self.device = 'cuda'
        
    # sentence-transformers API
    def encode(self, sentences, batch_size=32, show_progress_bar=None, convert_to_numpy=True, convert_to_tensor=True):
        with torch.no_grad():
            def _text_length(text):
                if isinstance(text, dict):              #{key: value} case
                    return len(next(iter(text.values())))
                elif not hasattr(text, '__len__'):      #Object has no len() method
                    return 1
                elif len(text) == 0 or isinstance(text[0], int):    #Empty string or list of ints
                    return len(text)
                else:
                    return sum([len(t) for t in text])      #Sum of length of individual strings

            all_embeddings = []
            length_sorted_idx = np.argsort([_text_length(sen) for sen in sentences])
            sentences_sorted = [sentences[idx] for idx in length_sorted_idx]

            for start_index in range(0, len(sentences), batch_size):
                sentences_batch = sentences_sorted[start_index:start_index+batch_size]
                embeddings = self.encode_text(sentences_batch, projection=False).cpu()
                all_embeddings.extend(embeddings)
            all_embeddings = [all_embeddings[idx] for idx in np.argsort(length_sorted_idx)]

            if convert_to_tensor:
                all_embeddings = torch.stack(all_embeddings)
            elif convert_to_numpy:
                all_embeddings = np.asarray([emb.numpy() for emb in all_embeddings])
        return all_embeddings
    
    def encode_text(self, texts, projection=False):

        encoded_input = self.tokenizer(texts, padding=True, truncation=True,return_tensors="pt")
        encoded_input = {
            'input_ids': encoded_input['input_ids'].to(self.device),
            'attention_mask': encoded_input['attention_mask'].to(self.device)
            }
        outputs = self.text_backbone(**encoded_input, output_hidden_states=True, return_dict=True)
        last_hidden = outputs.last_hidden_state
        pooler_output = outputs.pooler_output
        hidden_states = outputs.hidden_states

        # 'cls'
        text_features = pooler_output.cpu()
        # text_features = mean_pooling(text_features, encoded_input['attention_mask'])

        return text_features
        

def mean_pooling(model_output, attention_mask):
    token_embeddings = model_output[0] #First element of model_output contains all token embeddings
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(input_mask_expanded.sum(1), min=1e-9)


class WrappedOpenCLIPTransformer(nn.Module):
    def __init__(self, model_name) -> None:
        super().__init__()
        CLIP_model, preprocess_train, preprocess_val = open_clip.create_model_and_transforms(
            model_name=model_name,
            pretrained='openai',
            precision='amp',
            device='cuda',
            args=None
        )
        self.device = 'cuda'
        self.text_backbone = CLIP_model
        self.tokenizer = open_clip.tokenize
        
    def encode(self, sentences, batch_size=32, show_progress_bar=None, convert_to_numpy=True, convert_to_tensor=True):
        with torch.no_grad():
            def _text_length(text):
                if isinstance(text, dict):              #{key: value} case
                    return len(next(iter(text.values())))
                elif not hasattr(text, '__len__'):      #Object has no len() method
                    return 1
                elif len(text) == 0 or isinstance(text[0], int):    #Empty string or list of ints
                    return len(text)
                else:
                    return sum([len(t) for t in text])      #Sum of length of individual strings

            all_embeddings = []
            length_sorted_idx = np.argsort([_text_length(sen) for sen in sentences])
            sentences_sorted = [sentences[idx] for idx in length_sorted_idx]

            iterator = tqdm.tqdm(range(0, len(sentences), batch_size)) if show_progress_bar else range(0, len(sentences), batch_size)
            for start_index in iterator:
                sentences_batch = sentences_sorted[start_index:start_index+batch_size]
                embeddings = self.encode_text(sentences_batch, projection=True).cpu()
                all_embeddings.extend(embeddings)
            all_embeddings = [all_embeddings[idx] for idx in np.argsort(length_sorted_idx)]

            if convert_to_tensor:
                all_embeddings = torch.stack(all_embeddings)
            elif convert_to_numpy:
                all_embeddings = np.asarray([emb.numpy() for emb in all_embeddings])
        return all_embeddings
    
    def encode_text(self, texts, projection=False):
        with torch.no_grad():
            texts = self.tokenizer(texts, context_length=77).to(self.device)
            def open_clip_forward(texts):
                x = self.text_backbone.token_embedding(texts)  # [batch_size, n_ctx, d_model] (64, 77-args.n_prompts, 512)
                x = x + self.text_backbone.positional_embedding
                x = x.permute(1, 0, 2)  # NLD -> LND
                x = self.text_backbone.transformer(x, attn_mask=self.text_backbone.attn_mask)
                x = x.permute(1, 0, 2)  # LND -> NLD
                x = self.text_backbone.ln_final(x) # [batch_size, n_ctx, transformer.width]
                # take features from the eot embedding (eot_token is the highest number in each sequence)
                x = x[torch.arange(x.shape[0]), texts.argmax(dim=-1)] @ self.text_backbone.text_projection
                return x
            text_features = open_clip_forward(texts)
        return text_features


""" usage
conda activate vlkd
cd /data/codes/ProtoRKD
export PYTHONPATH="src"
eval $(curl -s http://deploy.i.brainpp.cn/httpproxy)
ulimit -n 65536
python src/utils/eval_PLM.py --text-model 'princeton-nlp/sup-simcse-roberta-large'
"""

if __name__=='__main__':

    args = parse_args()
    args.nlp_eval_frequency = 1
    args.distributed = False
    args.eval_data_dir='/data/Datasets'
    args.fast_evaluation=False
    args.linear_prob_mode='pytorch'
    args.batch_size=2048
    num_workers=8
    model_name = args.text_model

    huggingface_transformers = [
        'bert-base-uncased',
        'bert-base-cased',
        'bert-large-uncased',
        'bert-large-cased',
        'roberta-base',
        'roberta-large',
        'roberta-large-mnli',
        'facebook/muppet-roberta-large',
        'facebook/contriever',
        'facebook/contriever-msmarco',
        'Jean-Baptiste/roberta-large-ner-english',
        'princeton-nlp/unsup-simcse-roberta-large',
        'princeton-nlp/sup-simcse-roberta-large',
        'xlm-roberta-large',
        'xlm-roberta-large-finetuned-conll03-english',
        'deepset/xlm-roberta-large-squad2',
        'joeddav/xlm-roberta-large-xnli',
        'sentence-transformers/distiluse-base-multilingual-cased-v2',
        'sentence-transformers/paraphrase-distilroberta-base-v2',
        'sentence-transformers/paraphrase-MiniLM-L6-v2',
        'sentence-transformers/msmarco-distilbert-base-tas-b',
        'sentence-transformers/all-MiniLM-L12-v1',
        'sentence-transformers/all-mpnet-base-v2',
        'sentence-transformers/all-roberta-large-v1',
    ]

    sentence_transformers = [
        'average_word_embeddings_glove.6B.300d',
        'average_word_embeddings_komninos',
    ]

    open_clip_transformers = [
        'RN50',
        'RN101',
        'RN50x4',
        'RN50x16',
        'ViT-B-32',
        'ViT-B-16',
        'ViT-L-14',
        # 'RN50x64',
        'ViT-L-14-336'
    ]

    #for model_name in huggingface_transformers:
    if model_name in huggingface_transformers:
        model = WrappedHuggingfaceTransformer(model_name).cuda()
    elif model_name in sentence_transformers:
        model = SentenceTransformer(model_name).cuda()
    elif model_name in open_clip_transformers:
        model = WrappedOpenCLIPTransformer(model_name).cuda()
    else:
        raise 'wtf?'


    results = nlp_eval(model, epoch=0, args=args)
    results['model'] = model_name
    # logging.info( f"Finished evaluation of {model_name}\n" + "\n".join([f"\t{k}\t{v}" for k, v in results.items()]))

    results = pd.DataFrame(results, index = [0])
    print(results)
        
    results.to_csv(f'PLM_evaluations/sts-{model_name.replace("/", "-")}.csv', sep='\t', index=None)
